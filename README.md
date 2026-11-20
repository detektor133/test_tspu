# test_tspu: native Mihomo VPS test stand

Automated installation on a **clean Ubuntu 22.04/24.04 VPS** (amd64/arm64) without Docker.
Installs **Mihomo v1.19.34** and the **m-ui v0.3.3** control panel, both SHA256-verified from GitHub Releases. m-ui runs the Mihomo binary; do not install a second Mihomo systemd service.

## Install

```bash
git clone https://github.com/detektor133/test_tspu.git
cd test_tspu
sudo bash deploy.sh
```

Prerequisites: Ubuntu VPS with public IPv4, working DNS/HTTPS to GitHub, root/sudo access, and SSH keys. Deployment changes system network parameters; take a VPS snapshot first. No VPS tests have been performed yet.

The installer prints **one-time panel credentials and a random panel path**. Keep them private. The web UI is bound to `127.0.0.1:2053`, so from your laptop use:

```bash
ssh -L 2053:127.0.0.1:2053 user@VPS_IP
```

Then open the printed URL with `127.0.0.1` instead of the VPS IP. Do not publicly expose the panel or Mihomo control API.

Add inbound listeners and clients through m-ui. The port and protocol settings for each listener are created there, not hardcoded by deployment scripts. For TCP/UDP listener ports, configure your provider firewall (and UFW, if enabled) separately. **m-ui does not turn your server into a VPN without creating inbounds.**

## Optional UFW

UFW is **off by default** to avoid SSH lockouts. Only enable on a new VPS when sure of the actual SSH port:

```bash
sudo bash deploy.sh --firewall --ssh-port 22
sudo ufw allow 443/tcp  # example: only after configuring corresponding inbound
sudo ufw allow 443/udp  # example: only if your chosen UDP protocol requires it
```

If UFW was already enabled, inspect existing rules before deployment. The script does not remove existing rules or change SSH daemon settings.

## Network tuning

`deploy.sh` records baseline values in `/var/lib/test-tspu/before.txt` and writes only `/etc/sysctl.d/90-test-tspu.conf`:

- `bbr` with `fq` (only if the kernel advertises BBR): TCP congestion control/packet pacing; does **not** accelerate UDP/QUIC directly.
- `somaxconn` and `tcp_max_syn_backlog` 4096: server accept/SYN queue ceilings.
- `rmem_max`/`wmem_max` 8 MiB: maximum socket buffers (not a guarantee of higher throughput).
- m-ui `systemd` `LimitNOFILE=65536`, `TasksMax=4096`.

**Performance is not guaranteed to improve**: measure before and after in both networks, and compare TCP and UDP independently. This does not alter MTU, disable kernel protections or install a custom kernel.

```bash
sudo bash deploy.sh --check
sudo bash deploy.sh --rollback-tuning
```

Rollback does not uninstall m-ui or remove firewall rules. Re-running `deploy.sh` keeps existing Mihomo core and m-ui data/credentials; it does not silently upgrade installations.

## Verification

```bash
sudo systemctl status m-ui --no-pager
sudo journalctl -u m-ui -n 80 --no-pager
sudo ss -lntup
sudo bash deploy.sh --check
```

The full deployment is intentionally **not claimed as VPS-tested**. In CI, syntax and static checks run on the script. When a VPS becomes available, verify installation, fresh/repeated execution, SSH tunnel, inbound creation, traffic, and rollback.

## Sources

- [Mihomo releases](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.34)
- [m-ui releases](https://github.com/RomanovCaesar/m-ui/releases/tag/v0.3.3)
- [m-ui installer and documentation](https://github.com/RomanovCaesar/m-ui/blob/main/README.md)
- [Google BBR documentation](https://github.com/google/bbr/blob/master/Documentation/bbr-quick-start.md)
- [Linux sysctl networking documentation](https://docs.kernel.org/networking/ip-sysctl.html)