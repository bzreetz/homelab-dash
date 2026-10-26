# Homelab dashboard

A live dashboard for a Proxmox server on HPE hardware. Open it in any browser on your network (iPad, laptop, TV) and you get:

- CPU, memory, power draw, inlet temperature and network traffic, each with a one-hour trend
- Every VM and container with its status, CPU, memory and uptime, plus start, shut down, reboot and force stop
- All six fans (spinning at their real speed), every temperature sensor, both power supplies
- Storage pools and the physical drives, shown in the server's drive bays with SMART health
- Reboot or shut down the host, turn the blue locator light on and off, and power the server on through iLO
- Optional ping checks for other things on your network

Want to see it before setting anything up? Run `python3 server.py --demo` and open `http://localhost:8080`.

It's one small Python file with no packages to install. Proxmox numbers come from the Proxmox API; power, fans, temperatures and the locator light come from iLO 4 over Redfish.

---

## Setup

Takes about 15 minutes. You'll make a small container for the dashboard, an API token in Proxmox, and a login in iLO.

### 1. Make a container for it

In the Proxmox web UI:

1. **local (pve) → CT Templates → Templates**, download **debian-12-standard** (or 13).
2. **Create CT**: any hostname (`dashboard`), set a root password, pick that template. 1 core, 512 MiB memory, 4 GiB disk is plenty. Leave it unprivileged.
3. Network: DHCP is fine, but reserve its address in the Orbi app so the link never changes.
4. Start it and note its IP address (shown on the container's **Summary** page).

### 2. Create a Proxmox API token

Open the **pve → Shell** in the Proxmox web UI and paste:

```bash
pveum role add Dashboard --privs "Sys.Audit Sys.PowerMgmt VM.Audit VM.PowerMgmt Datastore.Audit"
pveum user add dashboard@pve --comment "Homelab dashboard"
pveum acl modify / --users dashboard@pve --roles Dashboard
pveum user token add dashboard@pve dash --privsep 0
```

The last command prints a table. Copy the **value** (looks like `a1b2c3d4-...`). Proxmox only shows it once.

Want look-only, no buttons? Leave out `Sys.PowerMgmt` and `VM.PowerMgmt`.

### 3. Create an iLO login

1. Open iLO in a browser (the address of the server's dedicated iLO port).
2. **Administration → User Administration → New**.
3. Use `dashboard` for both User Name and Login Name, set a password, and tick only **Virtual Power and Reset**. (iLO 4 has no separate login checkbox; every user can log in.) If the locator light button later says permission denied, also tick **Configure iLO Settings**.
4. Click **Add User**.

iLO 4 needs firmware **2.30 or newer** for this to work (the last release is 2.82). The version is on the iLO **Information → Overview** page.

### 4. Install

In the **pve → Shell**, go into the container (replace `105` with your container's ID):

```bash
pct enter 105
```

Now inside the container:

```bash
apt update && apt install -y git
git clone https://github.com/bzreetz/homelab-dash.git /opt/homelab-dash
cd /opt/homelab-dash && ./install.sh
nano /opt/homelab-dash/config.json
```

Fill in the Proxmox address and token, the iLO address and login, and pick an `admin_pin` (see below). Save with Ctrl+O, Enter, Ctrl+X, then:

```bash
systemctl restart homelab-dash
```

Open `http://<container-ip>:8080`. It starts on its own whenever the container boots. In the container's **Options**, set **Start at boot** to Yes so it's always there.

### 5. Put it on your screens

- **iPad / iPhone:** open it in Safari, Share → **Add to Home Screen**. It opens full screen like an app.
- **TV:** open it in the TV's browser (Silk on Fire TV works). Big screens get a three-column layout that fits most things without scrolling.
- **From outside the house over Tailscale:** the easiest way is to let the Proxmox host share your home network with your tailnet. On the pve shell run `tailscale set --advertise-routes=192.168.1.0/24` (your network's range), then approve the route in the Tailscale admin console. Your phone can then open the same `http://<container-ip>:8080` link from anywhere.

---

## config.json

| Setting | What it does |
|---|---|
| `port` | Port the page is served on. Default 8080. |
| `title` | Name in the top left. Blank uses the Proxmox node name. |
| `admin_pin` | PIN for the buttons. **Leave blank to turn all controls off** (read-only dashboard). |
| `temp_unit` | `"C"` or `"F"`. |
| `electricity_rate` | Your price per kWh, for the monthly cost estimate. Check your power bill; `0` hides the cost. |
| `drive_bays` | How many bays to draw on the front panel. The DL380 Gen9 8SFF has 8. |
| `proxmox.host` | `https://<proxmox-ip>:8006` |
| `proxmox.token_id` | `dashboard@pve!dash` if you used the commands above. |
| `proxmox.token_secret` | The value from step 2. |
| `proxmox.node` | Your node name (`pve`). Blank picks it automatically. |
| `ilo.host` | `https://<ilo-ip>` |
| `ilo.username`, `ilo.password` | The iLO login from step 3. |
| `devices` | Other things to show as online or offline. Each needs `name` and `host`. Add `"port": 22` to check a port instead of ping (useful for PCs that block ping). Use `[]` for none. |
| `verify_ssl` | Leave `false`. Proxmox and iLO use self-signed certificates. |

Remove the `ilo` or `proxmox` section (or set `"enabled": false` in it) if you don't have one of them; the page shows what it can.

---

## Good to know

- **Drive bays:** Proxmox doesn't report which physical bay a drive sits in, so drives fill the bays in order (sda, sdb, ...). Hover or tap a bay for the model and health.
- **Power on only helps if the dashboard runs somewhere else.** If it lives in a container on the server, it's off when the server is off. Run it on any other always-on Linux box (a Raspberry Pi works) and the Power on button works for real.
- **The PIN** is remembered in that browser tab until you close it. Five wrong tries locks the buttons for five minutes.
- **Don't port-forward this to the internet.** Use Tailscale. The PIN keeps people on your network from rebooting things, but it's not meant to face the whole internet.
- **History** (the little charts) covers the last hour. CPU and memory fill in right away from Proxmox; power and temperature build up while the dashboard runs.

## When something's wrong

The page lists problems right under the server name. For more detail, inside the container:

```bash
journalctl -u homelab-dash -f
```

| Message | Fix |
|---|---|
| Proxmox rejected the login (401) | The token ID or secret in config.json is wrong. Make a new token: `pveum user token remove dashboard@pve dash`, then the add command again. |
| Proxmox refused ... (403) | The role is missing a permission. Re-run the `pveum role add` line as `pveum role modify Dashboard --privs "..."`. |
| Can't reach Proxmox / iLO | Wrong IP, or the container can't reach that network. Try `curl -k https://<ip>:8006` from inside the container. |
| iLO has nothing at /redfish/v1/... (404) | iLO firmware is older than 2.30. Update it. |
| Drives section asks for Sys.Audit | The token can't read disk info. Check the role has `Sys.Audit`. |

## Updating

Inside the container:

```bash
/opt/homelab-dash/update.sh
```

It pulls the latest version from GitHub and restarts the dashboard. Your `config.json` isn't in the repo, so updates never touch it.

## Files

- `server.py` – the whole backend (Python standard library only)
- `static/index.html` – the dashboard page
- `static/fonts/` – Barlow and Barlow Condensed, bundled so it works without internet (SIL Open Font License)
- `config.example.json` – starting point for `config.json`
- `install.sh` – sets it up as a service that starts on boot
- `update.sh` – pulls the latest version and restarts the service
