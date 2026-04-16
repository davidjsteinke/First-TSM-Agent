# VM Setup Guide — TSM Agent on Oracle Cloud

This guide migrates the TSM agent to an Oracle Cloud free-tier VM so it runs 24/7 independently of your local machine.

---

## Phase A — Create Oracle Account and Provision VM

Do this manually in your browser before running the migration script.

1. **Create Oracle Cloud account** at cloud.oracle.com  
   Use the Always Free tier — no credit card charges for the VM.

2. **Provision a Compute VM**
   - Navigate to: Compute → Instances → Create Instance
   - Image: **Ubuntu 22.04 LTS** (Canonical)
   - Shape: **VM.Standard.A1.Flex** (ARM, free tier) — 1 OCPU, 6 GB RAM  
   - SSH key: upload your existing public key (or generate a new pair)
   - Leave other settings at defaults
   - Click **Create**

3. **Note the public IP address** shown on the instance details page  
   You will pass this as `VM_IP` to the migration script.

4. **Open firewall ports** (required for the dashboard)
   - In Oracle Cloud: Networking → Virtual Cloud Networks → your VCN → Security Lists
   - Add Ingress rule: Protocol TCP, Destination Port 5000, Source 0.0.0.0/0
   - (Optional) Also open port 80/443 if you set up Cloudflare later

---

## Phase B — Run the Migration Script

With the VM IP address from Phase A, run this from your local machine:

```bash
VM_IP=<your-vm-ip> bash ~/tsm-agent/vm_migrate.sh
```

The script does the following automatically:
1. Installs Python 3.11, git, pip, ntfs-3g on the VM
2. Clones the GitHub repo to `~/tsm-agent/` on the VM
3. Installs all Python dependencies from `requirements.txt`
4. Copies `.env` from your local machine to the VM securely
5. Sets up and enables systemd user timers (main agent + Discord alerts)
6. Starts a Flask web server on port 5000 serving `dashboard.html`

---

## How to Verify After Migration

SSH into the VM and check:

```bash
ssh ubuntu@<your-vm-ip>

# Check timers are running
systemctl --user list-timers

# Check the dashboard server
systemctl --user status tsm-dashboard.service

# View live logs
tail -f ~/tsm-agent/logs/agent.log
```

Open your browser at `http://<your-vm-ip>:5000` — the dashboard should load.

---

## Cloudflare Tunnel (Optional — for HTTPS Access)

After the VM is running, you can expose the dashboard securely over HTTPS with a Cloudflare Tunnel (no port forwarding required, free):

```bash
# 1. Install cloudflared on the VM
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb
sudo dpkg -i cf.deb

# 2. Log in to Cloudflare (opens a browser link — copy/paste from terminal)
cloudflared tunnel login

# 3. Create a named tunnel
cloudflared tunnel create tsm-dashboard

# 4. Start the tunnel (this exposes http://localhost:5000 to the internet)
cloudflared tunnel --url http://localhost:5000
```

Cloudflare will give you a public `*.trycloudflare.com` URL. You can also configure a custom domain in your Cloudflare dashboard.

To run the tunnel persistently, set it up as a systemd service (see cloudflared docs).

---

## Note on the Lua File

The TradeSkillMaster.lua file lives on the local Windows machine and is NOT available on the VM. The agent currently reads from it to get:
- Personal transaction history (csvBuys, csvSales)
- TSM market values

**Future session**: Live AH data via the Blizzard Auction House API will replace the Lua file as the primary data source. At that point, the VM will be fully self-contained and the dashboard will update automatically without needing the local machine.

Until then, you can periodically sync the latest `tsm_data.json` to the VM:

```bash
scp ~/tsm_data.json ubuntu@<your-vm-ip>:~/tsm_data.json
```
