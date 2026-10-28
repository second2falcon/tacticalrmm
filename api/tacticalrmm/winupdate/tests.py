# from itertools import cycle
from unittest.mock import patch

from model_bakery import baker

from tacticalrmm.test import TacticalTestCase

from .models import WinUpdate
from .serializers import WinUpdateSerializer

base_url = "/winupdate"


class TestWinUpdateViews(TacticalTestCase):
    def setUp(self):
        self.authenticate()
        self.setup_coresettings()

    @patch("agents.models.Agent.nats_cmd")
    def test_run_update_scan(self, nats_cmd):
        agent = baker.make_recipe("agents.agent")
        url = f"{base_url}/{agent.agent_id}/scan/"
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200)
        nats_cmd.assert_called_with({"func": "getwinupdates"}, wait=False)

        self.check_not_authenticated("post", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_install_updates(self, nats_cmd):
        agent = baker.make_recipe("agents.agent")
        baker.make("winupdate.WinUpdate", agent=agent, _quantity=4)
        baker.make("winupdate.WinUpdatePolicy", agent=agent)
        url = f"{base_url}/{agent.agent_id}/install/"
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200)
        nats_cmd.assert_called_once()

        self.check_not_authenticated("post", url)

    def test_get_winupdates(self):
        agent = baker.make_recipe("agents.agent")
        baker.make("winupdate.WinUpdate", agent=agent, _quantity=4)

        # test a call where agent doesn't exist
        resp = self.client.get(f"{base_url}/234kj34lk/", format="json")
        self.assertEqual(resp.status_code, 404)

        url = f"{base_url}/{agent.agent_id}/"
        resp = self.client.get(url, format="json")
        updates = WinUpdate.objects.filter(agent=agent).order_by("-id", "installed")
        serializer = WinUpdateSerializer(updates, many=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 4)
        self.assertEqual(resp.data, serializer.data)

        self.check_not_authenticated("get", url)

    def test_edit_winupdate(self):
        agent = baker.make_recipe("agents.agent")
        winupdate = baker.make("winupdate.WinUpdate", agent=agent)
        url = f"{base_url}/{winupdate.pk}/"

        data = {"policy": "inherit"}
        # test a call where winupdate doesn't exist
        resp = self.client.put(f"{base_url}/500/", data, format="json")
        self.assertEqual(resp.status_code, 404)

        resp = self.client.put(url, data, format="json")
        self.assertEqual(resp.status_code, 200)

        self.check_not_authenticated("put", url)


class TestWinUpdatePermissions(TacticalTestCase):
    def setUp(self):
        self.setup_coresettings()
        self.setup_client()

    @patch("agents.models.Agent.nats_cmd", return_value="ok")
    def test_get_scan_install_permissions(self, nats_cmd):
        agent = baker.make_recipe("agents.agent")
        baker.make("winupdate.WinUpdatePolicy", agent=agent)
        unauthorized_agent = baker.make_recipe("agents.agent")
        baker.make("winupdate.WinUpdatePolicy", agent=unauthorized_agent)
        baker.make("winupdate.WinUpdate", agent=agent)
        baker.make("winupdate.WinUpdate", agent=unauthorized_agent)

        test_data = [
            {"url": f"{base_url}/{agent.agent_id}/", "method": "get"},
            {"url": f"{base_url}/{agent.agent_id}/scan/", "method": "post"},
            {"url": f"{base_url}/{agent.agent_id}/install/", "method": "post"},
        ]

        for data in test_data:
            # test superuser
            self.check_authorized_superuser(data["method"], data["url"])

            # test user with no roles
            user = self.create_user_with_roles([])
            self.client.force_authenticate(user=user)

            self.check_not_authorized(data["method"], data["url"])

            # test with correct role
            user.role.can_manage_winupdates = True
            user.role.save()

            self.check_authorized(data["method"], data["url"])

            # test limiting user to site
            user.role.can_view_sites.set([agent.site])
            self.check_authorized(data["method"], data["url"])

            user.role.can_view_sites.set([unauthorized_agent.site])
            self.check_not_authorized(data["method"], data["url"])

            self.client.logout()

    def test_edit_winupdate_permissions(self):
        agent = baker.make_recipe("agents.agent")
        baker.make("winupdate.WinUpdatePolicy", agent=agent)
        update = baker.make("winupdate.WinUpdate", agent=agent)

        unauthorized_agent = baker.make_recipe("agents.agent")
        baker.make("winupdate.WinUpdatePolicy", agent=unauthorized_agent)
        baker.make("winupdate.WinUpdate", agent=unauthorized_agent)

        url = f"{base_url}/{update.pk}/"

        # test superuser
        self.check_authorized_superuser("put", url)

        # test user with no roles
        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)

        self.check_not_authorized("put", url)

        # test with correct role
        user.role.can_manage_winupdates = True
        user.role.save()

        self.check_authorized("put", url)

        # test limiting user to site
        user.role.can_view_sites.set([agent.site])
        self.check_authorized("put", url)

        user.role.can_view_sites.set([unauthorized_agent.site])
        self.check_not_authorized("put", url)


class WinupdateTasks(TacticalTestCase):
    def setUp(self):
        self.setup_coresettings()

        site = baker.make("clients.Site")
        self.online_agents = baker.make_recipe(
            "agents.online_agent", site=site, _quantity=2
        )
        self.offline_agent = baker.make_recipe("agents.agent", site=site)

    # @patch("agents.models.Agent.nats_cmd")
    # @patch("time.sleep")
    # def test_auto_approve_task(self, mock_sleep, nats_cmd):
    #     from .tasks import auto_approve_updates_task

    #     # Setup data
    #     baker.make_recipe(
    #         "winupdate.winupdate",
    #         agent=cycle(
    #             [self.online_agents[0], self.online_agents[1], self.offline_agent]
    #         ),
    #         _quantity=20,
    #     )
    #     baker.make_recipe(
    #         "winupdate.winupdate_approve",
    #         agent=cycle(
    #             [self.online_agents[0], self.online_agents[1], self.offline_agent]
    #         ),
    #         _quantity=3,
    #     )

    #     # run task synchronously
    #     auto_approve_updates_task()

    #     # make sure the check_for_updates_task was run once for each online agent
    #     self.assertEqual(nats_cmd.call_count, 2)

    #     # check if all of the created updates were approved
    #     winupdates = WinUpdate.objects.all()
    #     for update in winupdates:
    #         self.assertEqual(update.action, "approve")


class TestLinuxUpdates(TacticalTestCase):
    """apt updates reported through the windows update api (#2556)"""

    def setUp(self):
        self.authenticate()
        self.setup_coresettings()
        self.agent = baker.make_recipe("agents.agent", plat="linux")
        self.authenticate_agent(self.agent)

    def _pkg(self, guid, severity=""):
        return {
            "guid": guid,
            "title": guid,
            "description": "Debian-Security:12/stable-security",
            "severity": severity,
            "categories": ["Updates"],
            "category_ids": [],
            "kb_article_ids": [],
            "more_info_urls": [],
            "support_url": "",
            "revision_number": 0,
            "installed": False,
            "downloaded": False,
        }

    def test_scan_results(self):
        url = "/api/v3/winupdates/"
        data = {
            "agent_id": self.agent.agent_id,
            "wua_updates": [
                self._pkg("openssl=3.0.15-1~deb12u1", "Important"),
                self._pkg("tzdata=2024b-0+deb12u1"),
            ],
        }
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            set(self.agent.winupdates.values_list("kb", flat=True)),
            {"openssl", "tzdata"},
        )

        # an empty scan means everything is up to date
        r = self.client.post(url, {**data, "wua_updates": []}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.agent.winupdates.exists())

    @patch("agents.models.Agent.nats_cmd")
    def test_scan_allowed_for_linux_only(self, nats_cmd):
        self.client.force_authenticate(user=self.john)
        r = self.client.post(f"{base_url}/{self.agent.agent_id}/scan/")
        self.assertEqual(r.status_code, 200)

        mac = baker.make_recipe("agents.agent", plat="darwin")
        r = self.client.post(f"{base_url}/{mac.agent_id}/scan/")
        self.assertEqual(r.status_code, 400)
