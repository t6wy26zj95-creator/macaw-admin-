"""Minimal OpenStack client for HostVDS (Keystone v3, Nova, Neutron, Glance) over plain REST.

Used by bot.py to create a new server and delete an old one. No extra dependencies (aiohttp
comes with aiogram).
"""
import asyncio
import base64
import json
import re

import aiohttp


class CloudError(Exception):
    pass


OPENRC_KEYS = ("OS_AUTH_URL", "OS_USERNAME", "OS_PROJECT_ID", "OS_PROJECT_NAME", "OS_USER_DOMAIN_NAME",
               "OS_USER_DOMAIN_ID", "OS_PROJECT_DOMAIN_ID", "OS_PROJECT_DOMAIN_NAME", "OS_REGION_NAME",
               "OS_TENANT_ID", "OS_TENANT_NAME")


def parse_openrc(text: str) -> dict[str, str]:
    """Pull OS_* values out of an openrc.sh file. Values that reference variables ($...) are skipped."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'\s*(?:export\s+)?(OS_[A-Z_]+)=(.*)$', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if not val or "$" in val:
            continue
        out[key] = val
    # Old-style names
    if "OS_PROJECT_ID" not in out and "OS_TENANT_ID" in out:
        out["OS_PROJECT_ID"] = out["OS_TENANT_ID"]
    if "OS_PROJECT_NAME" not in out and "OS_TENANT_NAME" in out:
        out["OS_PROJECT_NAME"] = out["OS_TENANT_NAME"]
    out = {k: v for k, v in out.items() if k in OPENRC_KEYS}
    if "OS_AUTH_URL" not in out or "OS_USERNAME" not in out:
        # Not an openrc: accept loose text, e.g. the Identity URL from Horizon's API Access page plus the login.
        url = re.search(r"https?://[^\s'\"]+", text)
        login = re.search(r"\b(hostvds-[0-9a-fA-F-]{8,})\b", text)
        if url and "OS_AUTH_URL" not in out:
            out["OS_AUTH_URL"] = url.group(0)
        if login and "OS_USERNAME" not in out:
            out["OS_USERNAME"] = login.group(1)
    return out


def cloud_config(root_password: str, pubkey: str) -> str:
    """cloud-init user data: root password + our SSH key, root login allowed."""
    pw = json.dumps(root_password)  # JSON strings are valid YAML
    key = json.dumps(pubkey.strip())
    return f"""#cloud-config
disable_root: false
ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - name: root
      password: {pw}
      type: text
write_files:
  - path: /etc/ssh/sshd_config.d/01-macaw-root.conf
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
runcmd:
  - mkdir -p /root/.ssh && chmod 700 /root/.ssh
  - echo {key} >> /root/.ssh/authorized_keys
  - chmod 600 /root/.ssh/authorized_keys
  - systemctl restart ssh || systemctl restart sshd
"""


class Cloud:
    def __init__(self, cfg: dict[str, str]):
        self.cfg = cfg
        url = cfg["OS_AUTH_URL"].rstrip("/")
        if not url.endswith("/v3"):
            url += "/v3"
        self.auth_url = url
        self.token = None
        self.catalog = []

    def _session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    def _user(self) -> dict:
        c = self.cfg
        user = {"name": c["OS_USERNAME"], "password": c["OS_PASSWORD"]}
        user["domain"] = ({"id": c["OS_USER_DOMAIN_ID"]} if c.get("OS_USER_DOMAIN_ID")
                          else {"name": c.get("OS_USER_DOMAIN_NAME") or "Default"})
        return user

    async def discover_project(self):
        """No project given: log in unscoped and take the account's first project."""
        body = {"auth": {"identity": {"methods": ["password"], "password": {"user": self._user()}}}}
        async with self._session() as s:
            async with s.post(self.auth_url + "/auth/tokens", json=body) as r:
                if r.status != 201:
                    hint = " (wrong login or password?)" if r.status == 401 else ""
                    raise CloudError(f"HostVDS login failed: HTTP {r.status}{hint}")
                token = r.headers["X-Subject-Token"]
            async with s.get(self.auth_url + "/auth/projects", headers={"X-Auth-Token": token}) as r:
                projects = (await r.json(content_type=None) or {}).get("projects", [])
        if not projects:
            raise CloudError("Logged in, but the account has no project")
        self.cfg["OS_PROJECT_ID"] = projects[0]["id"]

    async def auth(self):
        c = self.cfg
        if not c.get("OS_PROJECT_ID") and not c.get("OS_PROJECT_NAME"):
            await self.discover_project()
        user = self._user()
        if c.get("OS_PROJECT_ID"):
            project = {"id": c["OS_PROJECT_ID"]}
        else:
            project = {"name": c["OS_PROJECT_NAME"]}
            project["domain"] = ({"id": c["OS_PROJECT_DOMAIN_ID"]} if c.get("OS_PROJECT_DOMAIN_ID")
                                 else {"name": c.get("OS_PROJECT_DOMAIN_NAME") or "Default"})
        body = {"auth": {"identity": {"methods": ["password"], "password": {"user": user}},
                         "scope": {"project": project}}}
        async with self._session() as s:
            async with s.post(self.auth_url + "/auth/tokens", json=body) as r:
                text = await r.text()
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
                if r.status != 201 or not isinstance(data, dict):
                    msg = (data or {}).get("error", {}).get("message", "") if isinstance(data, dict) else ""
                    hint = " (wrong login or password?)" if r.status == 401 else ""
                    raise CloudError(f"HostVDS login failed: HTTP {r.status}{hint} {msg}".strip())
                self.token = r.headers["X-Subject-Token"]
                self.catalog = data["token"].get("catalog", [])

    def endpoint(self, service_type: str) -> str:
        region = self.cfg.get("OS_REGION_NAME")
        for svc in self.catalog:
            if svc.get("type") != service_type:
                continue
            eps = [e for e in svc.get("endpoints", []) if e.get("interface") == "public"]
            if region:
                eps = [e for e in eps if e.get("region") == region or e.get("region_id") == region] or eps
            if eps:
                return eps[0]["url"].rstrip("/")
        raise CloudError(f"HostVDS has no '{service_type}' service in its catalog")

    async def call(self, method: str, service: str, path: str, body=None, ok=(200, 201, 202, 204)):
        if not self.token:
            await self.auth()
        base = self.endpoint(service)
        if service == "network" and not re.search(r"/v2\.0$", base):
            base += "/v2.0"
        if service == "image" and not re.search(r"/v2$", base):
            base += "/v2"
        headers = {"X-Auth-Token": self.token, "Accept": "application/json"}
        async with self._session() as s:
            async with s.request(method, base + path, json=body, headers=headers) as r:
                text = await r.text()
                if r.status not in ok:
                    raise CloudError(f"{method} {service}{path}: HTTP {r.status} {text[:300]}")
                return json.loads(text) if text.strip() else {}

    # ---- lookups ----

    async def flavors(self) -> list[dict]:
        data = await self.call("GET", "compute", "/flavors/detail")
        return sorted(data.get("flavors", []), key=lambda f: (f.get("ram", 0), f.get("vcpus", 0), f.get("disk", 0)))

    async def networks(self) -> list[dict]:
        nets = (await self.call("GET", "network", "/networks")).get("networks", [])
        subnets = (await self.call("GET", "network", "/subnets")).get("subnets", [])
        versions: dict[str, set] = {}
        for sn in subnets:
            versions.setdefault(sn["network_id"], set()).add(sn.get("ip_version"))
        for n in nets:
            v = versions.get(n["id"], set())
            n["_label"] = "+".join(f"IPv{x}" for x in sorted(v)) or "no subnets"
        return [n for n in nets if n.get("status", "ACTIVE") == "ACTIVE"]

    async def images(self) -> list[dict]:
        data = await self.call("GET", "image", "/images?limit=500&status=active")
        return data.get("images", [])

    @staticmethod
    def pick_ubuntu_2404(images: list[dict]) -> dict | None:
        cands = [i for i in images if re.search(r"ubuntu", i.get("name", ""), re.I)
                 and re.search(r"24\.04|noble", i.get("name", ""), re.I)]
        cands.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return cands[0] if cands else None

    # ---- servers ----

    async def create_server(self, name: str, flavor: dict, image_id: str, network_id: str,
                            user_data: str, volume_gb: int = 20) -> str:
        server = {"name": name, "flavorRef": flavor["id"], "networks": [{"uuid": network_id}],
                  "user_data": base64.b64encode(user_data.encode()).decode()}
        if flavor.get("disk", 0) == 0:  # flavor without local disk: boot from a volume
            server["block_device_mapping_v2"] = [{
                "boot_index": 0, "uuid": image_id, "source_type": "image", "destination_type": "volume",
                "volume_size": volume_gb, "delete_on_termination": True}]
        else:
            server["imageRef"] = image_id
        data = await self.call("POST", "compute", "/servers", {"server": server})
        return data["server"]["id"]

    async def get_server(self, server_id: str) -> dict:
        return (await self.call("GET", "compute", f"/servers/{server_id}"))["server"]

    @staticmethod
    def ipv4(server: dict) -> str | None:
        for addrs in (server.get("addresses") or {}).values():
            for a in addrs:
                if a.get("version") == 4:
                    return a["addr"]
        return None

    async def wait_active(self, server_id: str, timeout: int = 600) -> dict:
        for _ in range(timeout // 10):
            srv = await self.get_server(server_id)
            if srv.get("status") == "ERROR":
                fault = (srv.get("fault") or {}).get("message", "")
                raise CloudError(f"HostVDS reports the new server failed: {fault}")
            if srv.get("status") == "ACTIVE" and self.ipv4(srv):
                return srv
            await asyncio.sleep(10)
        raise CloudError("The new server did not become ready in 10 minutes")

    async def find_by_ip(self, ip: str) -> dict | None:
        data = await self.call("GET", "compute", "/servers/detail")
        for srv in data.get("servers", []):
            for addrs in (srv.get("addresses") or {}).values():
                if any(a.get("addr") == ip for a in addrs):
                    return srv
        return None

    async def delete_server(self, server_id: str) -> None:
        await self.call("DELETE", "compute", f"/servers/{server_id}")
