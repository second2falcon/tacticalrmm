import datetime as dt
import re
import uuid
from contextlib import suppress

from django.conf import settings
from django.db.models import (
    Count,
    Exists,
    OuterRef,
    Prefetch,
    Q,
    prefetch_related_objects,
)
from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime
from knox.models import AuthToken
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.models import Agent
from core.tasks import sync_mesh_consent_task
from core.utils import get_core_settings
from tacticalrmm.helpers import notify_error
from tacticalrmm.permissions import _has_partial_perm_on_client, _has_perm_on_site

from .models import (
    MESH_CONSENT_FIELDS,
    Client,
    ClientCustomField,
    Deployment,
    Site,
    SiteCustomField,
)
from .permissions import ClientsPerms, DeploymentPerms, SitesPerms
from .serializers import (
    ClientCustomFieldSerializer,
    ClientSerializer,
    DeploymentSerializer,
    SiteCustomFieldSerializer,
    SiteSerializer,
)


def _roles_left_unrestricted(*, client=None, site=None) -> list[str]:
    """
    Names of roles whose only allowed clients/sites are being deleted.
    An empty allow list means unrestricted, so deleting them would grant
    those roles access to every client (#2457).
    """
    from accounts.models import Role

    gone_clients = [client.pk] if client else []
    gone_sites = (
        list(client.sites.values_list("pk", flat=True)) if client else [site.pk]
    )

    roles = (
        Role.objects.filter(
            Q(can_view_clients__in=gone_clients) | Q(can_view_sites__in=gone_sites)
        )
        .exclude(is_superuser=True)
        .distinct()
    )
    return [
        role.name
        for role in roles
        if not role.can_view_clients.exclude(pk__in=gone_clients).exists()
        and not role.can_view_sites.exclude(pk__in=gone_sites).exists()
    ]


class GetAddClients(APIView):
    permission_classes = [IsAuthenticated, ClientsPerms]

    def get(self, request):
        clients = (
            Client.objects.order_by("name")
            .select_related("workstation_policy", "server_policy", "alert_template")
            .filter_by_role(request.user)  # type: ignore
            .prefetch_related(
                Prefetch(
                    "custom_fields",
                    queryset=ClientCustomField.objects.select_related("field"),
                ),
                Prefetch(
                    "sites",
                    queryset=Site.objects.order_by("name")
                    .select_related("client")
                    .filter_by_role(request.user)
                    .prefetch_related("custom_fields__field")
                    .annotate(
                        maintenance_mode=Exists(
                            Agent.objects.filter(
                                site=OuterRef("pk"), maintenance_mode=True
                            )
                        )
                    )
                    .annotate(agent_count=Count("agents")),
                    to_attr="filtered_sites",
                ),
            )
            .annotate(
                maintenance_mode=Exists(
                    Agent.objects.filter(
                        site__client=OuterRef("pk"), maintenance_mode=True
                    )
                )
            )
            .annotate(agent_count=Count("sites__agents"))
        )
        return Response(ClientSerializer(clients, many=True).data)

    def post(self, request):
        # create client
        client_serializer = ClientSerializer(data=request.data["client"])
        client_serializer.is_valid(raise_exception=True)
        client = client_serializer.save()

        # create site
        site_serializer = SiteSerializer(
            data={"client": client.id, "name": request.data["site"]["name"]}
        )

        # make sure site serializer doesn't return errors and save
        if site_serializer.is_valid():
            site_serializer.save()
        else:
            # delete client since site serializer was invalid
            client.delete()
            site_serializer.is_valid(raise_exception=True)

        if "initialsetup" in request.data.keys():
            core = get_core_settings()
            core.default_time_zone = request.data["timezone"]
            core.mesh_company_name = request.data["companyname"]
            core.save(update_fields=["default_time_zone", "mesh_company_name"])

        # save custom fields
        if "custom_fields" in request.data.keys():
            for field in request.data["custom_fields"]:
                custom_field = field
                custom_field["client"] = client.id

                serializer = ClientCustomFieldSerializer(data=custom_field)
                serializer.is_valid(raise_exception=True)
                serializer.save()

        # add user to allowed clients in role if restricted user created the client
        if request.user.role and request.user.role.can_view_clients.exists():
            request.user.role.can_view_clients.add(client)

        return Response(f"{client.name} was added")


class GetUpdateDeleteClient(APIView):
    permission_classes = [IsAuthenticated, ClientsPerms]

    def get(self, request, pk):
        client = get_object_or_404(Client, pk=pk)

        prefetch_related_objects(
            [client],
            Prefetch(
                "sites",
                queryset=Site.objects.order_by("name")
                .select_related("client")
                .filter_by_role(request.user)
                .prefetch_related("custom_fields__field")
                .annotate(
                    maintenance_mode=Exists(
                        Agent.objects.filter(site=OuterRef("pk"), maintenance_mode=True)
                    )
                )
                .annotate(agent_count=Count("agents")),
                to_attr="filtered_sites",
            ),
        )
        return Response(ClientSerializer(client).data)

    def put(self, request, pk):
        client = get_object_or_404(Client, pk=pk)

        serializer = ClientSerializer(
            data=request.data["client"], instance=client, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        if MESH_CONSENT_FIELDS & request.data["client"].keys():
            sync_mesh_consent_task.delay()

        # update custom fields
        if "custom_fields" in request.data.keys():
            for field in request.data["custom_fields"]:
                custom_field = field
                custom_field["client"] = pk

                if ClientCustomField.objects.filter(field=field["field"], client=pk):
                    value = ClientCustomField.objects.get(
                        field=field["field"], client=pk
                    )
                    serializer = ClientCustomFieldSerializer(
                        instance=value, data=custom_field
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                else:
                    serializer = ClientCustomFieldSerializer(data=custom_field)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

        return Response("{client} was updated")

    def delete(self, request, pk):
        client = get_object_or_404(Client, pk=pk)
        if roles := _roles_left_unrestricted(client=client):
            return notify_error(
                f"{client.name} is the last allowed client for role(s) "
                f"{', '.join(roles)}. Deleting it would give them access to "
                "all clients. Edit the role(s) first."
            )

        agent_count = client.live_agent_count

        # only run tasks if it affects clients
        if agent_count > 0 and "move_to_site" in request.query_params.keys():
            agents = Agent.objects.filter(site__client=client)
            site = get_object_or_404(Site, pk=request.query_params["move_to_site"])
            if not _has_perm_on_site(request.user, site.pk):
                raise PermissionDenied()

            agents.update(site=site)

        elif agent_count > 0:
            return notify_error(
                "Agents exist under this client. There needs to be a site specified to move existing agents to"
            )

        client.delete()
        return Response(f"{client.name} was deleted")


class GetAddSites(APIView):
    permission_classes = [IsAuthenticated, SitesPerms]

    def get(self, request):
        sites = Site.objects.filter_by_role(request.user)  # type: ignore
        return Response(SiteSerializer(sites, many=True).data)

    def post(self, request):
        if not _has_partial_perm_on_client(
            request.user, request.data["site"]["client"]
        ):
            raise PermissionDenied()

        serializer = SiteSerializer(data=request.data["site"])
        serializer.is_valid(raise_exception=True)
        site = serializer.save()

        # save custom fields
        if "custom_fields" in request.data.keys():
            for field in request.data["custom_fields"]:
                custom_field = field
                custom_field["site"] = site.id

                serializer = SiteCustomFieldSerializer(data=custom_field)
                serializer.is_valid(raise_exception=True)
                serializer.save()

        # add user to allowed sites in role if restricted user created the client
        if request.user.role and request.user.role.can_view_sites.exists():
            request.user.role.can_view_sites.add(site)

        return Response(f"Site {site.name} was added!")


class GetUpdateDeleteSite(APIView):
    permission_classes = [IsAuthenticated, SitesPerms]

    def get(self, request, pk):
        site = get_object_or_404(Site, pk=pk)
        return Response(SiteSerializer(site).data)

    def put(self, request, pk):
        site = get_object_or_404(Site, pk=pk)

        if "client" in request.data["site"].keys() and (
            site.client.id != request.data["site"]["client"]
            and site.client.sites.count() == 1
        ):
            return notify_error("A client must have at least one site")

        serializer = SiteSerializer(
            instance=site, data=request.data["site"], partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        if MESH_CONSENT_FIELDS & request.data["site"].keys():
            sync_mesh_consent_task.delay()

        # update custom field
        if "custom_fields" in request.data.keys():
            for field in request.data["custom_fields"]:
                custom_field = field
                custom_field["site"] = pk

                if SiteCustomField.objects.filter(field=field["field"], site=pk):
                    value = SiteCustomField.objects.get(field=field["field"], site=pk)
                    serializer = SiteCustomFieldSerializer(
                        instance=value, data=custom_field, partial=True
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                else:
                    serializer = SiteCustomFieldSerializer(data=custom_field)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

        return Response("Site was edited")

    def delete(self, request, pk):
        site = get_object_or_404(Site, pk=pk)
        if site.client.sites.count() == 1:
            return notify_error("A client must have at least 1 site.")

        if roles := _roles_left_unrestricted(site=site):
            return notify_error(
                f"{site.name} is the last allowed site for role(s) "
                f"{', '.join(roles)}. Deleting it would give them access to "
                "all clients. Edit the role(s) first."
            )

        # only run tasks if it affects clients
        agent_count = site.live_agent_count
        if agent_count > 0 and "move_to_site" in request.query_params.keys():
            agents = Agent.objects.filter(site=site)
            new_site = get_object_or_404(Site, pk=request.query_params["move_to_site"])
            if not _has_perm_on_site(request.user, new_site.pk):
                raise PermissionDenied()

            agents.update(site=new_site)

        elif agent_count > 0:
            return notify_error(
                "There needs to be a site specified to move the agents to"
            )

        site.delete()
        return Response(f"{site.name} was deleted")


class AgentDeployment(APIView):
    permission_classes = [IsAuthenticated, DeploymentPerms]

    def get(self, request):
        deps = Deployment.objects.filter_by_role(request.user)  # type: ignore
        return Response(DeploymentSerializer(deps, many=True).data)

    def post(self, request):
        if getattr(settings, "TRMM_INSECURE", False):
            return notify_error("Not available in insecure mode")

        from accounts.models import User

        site = get_object_or_404(Site, pk=request.data["site"])

        if not _has_perm_on_site(request.user, site.pk):
            raise PermissionDenied()

        installer_user = User.objects.filter(is_installer_user=True).first()

        try:
            expires = dt.datetime.strptime(
                request.data["expires"], "%Y-%m-%dT%H:%M:%S%z"
            )

        except Exception:
            return notify_error("expire date is invalid")

        obj, token = AuthToken.objects.create(
            user=installer_user, expiry=expires - djangotime.now()
        )

        flags = {
            "power": request.data["power"],
            "ping": request.data["ping"],
            "rdp": request.data["rdp"],
        }

        Deployment(
            site=site,
            expiry=expires,
            mon_type=request.data["agenttype"],
            goarch=request.data["goarch"],
            auth_token=obj,
            token_key=token,
            install_flags=flags,
        ).save()
        return Response("The deployment was added successfully")

    def delete(self, request, pk):
        d = get_object_or_404(Deployment, pk=pk)

        if not _has_perm_on_site(request.user, d.site.pk):
            raise PermissionDenied()

        with suppress(Exception):
            d.auth_token.delete()

        d.delete()
        return Response("The deployment was deleted")


class GenerateAgent(APIView):
    permission_classes = (AllowAny,)

    def get(self, request, uid):
        if getattr(settings, "TRMM_INSECURE", False):
            return notify_error("Not available in insecure mode")

        from tacticalrmm.utils import generate_winagent_exe

        try:
            _ = uuid.UUID(uid, version=4)
        except ValueError:
            return notify_error("invalid")

        d = get_object_or_404(Deployment, uid=uid)

        client = d.client.name.replace(" ", "").lower()
        site = d.site.name.replace(" ", "").lower()
        client = re.sub(r"([^a-zA-Z0-9]+)", "", client)
        site = re.sub(r"([^a-zA-Z0-9]+)", "", site)
        file_name = f"trmm-{client}-{site}-{d.mon_type}-{d.goarch}.exe"

        return generate_winagent_exe(
            client=d.client.pk,
            site=d.site.pk,
            agent_type=d.mon_type,
            rdp=d.install_flags["rdp"],
            ping=d.install_flags["ping"],
            power=d.install_flags["power"],
            goarch=d.goarch,
            token=d.token_key,
            api=f"https://{request.get_host()}",
            file_name=file_name,
        )
