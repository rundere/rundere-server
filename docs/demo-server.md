# Running a demo server

A Run·dere **demo server** is a read-only showcase (SPEC-8 D-198): one shared
login, fictional sample trips, and every write refused. It's how
demo.rundere.app lets App Review, testers and anyone curious look around.
Start from [`deploy/docker-compose.demo.example.yml`](../deploy/docker-compose.demo.example.yml).

## What demo mode does

- **Read-only.** Every write is refused except signing in and out. The page
  doesn't offer edit controls at all.
- **Closed doors.** MCP, OAuth, single sign-on, registration, password reset,
  ingest and admin answer 404.
- **Nothing goes out.** No email, no AI, no lookups: the self-hosted services are
  switched off, and the sample data carries its own weather, rates and places.
  The only outbound traffic is map tiles for the trip PDFs.
- **No visitor log.** uvicorn's per-request log is off (YONDER-277).
- **Kept current, never wiped.** The sample trips' dates move forward each night
  at 03:00 in `TX_DEMO_TIMEZONE`, and at every start.
- **Refuses to start** without sign-in, with mail or SSO configured, or on a
  database that holds any user other than the demo login. A real server can't be
  turned into a demo by accident.

## Isolation

A demo is a public, anonymous-friendly server, so keep it away from the rest of
your network:

1. **Its own network,** shared only with your reverse proxy
   (`docker network create --subnet 172.30.0.0/24 demo-edge`), never the network
   your other services or your database are on. Pin the demo's address in the
   stack (`ipv4_address`) so the firewall can name it.
2. **Its own DNS resolvers** (`dns:` in the stack), not your LAN's.
3. **A firewall** that stops the demo container from starting connections to
   private ranges, and to the host itself. Match the container's pinned address,
   **never the subnet**: the reverse proxy shares that subnet, and subnet rules
   would cut it off too.

An example with iptables. `DOCKER-USER` is the chain Docker leaves for you and
sees container traffic before Docker's own rules. Every rule is **inserted at a
fixed position**, because the chain ends with a catch-all `RETURN`:

```bash
#!/bin/sh
# Run·dere demo isolation. Usage: demo-firewall.sh [start|stop]  (idempotent)
set -e
DEMO=172.30.0.10                      # the demo container's pinned address
C="-m comment --comment rundere-demo"
for chain in DOCKER-USER INPUT; do
  iptables -S "$chain" | grep -- "rundere-demo" | sed 's/^-A/-D/' | while read -r r; do iptables $r; done
done
[ "${1:-start}" = "stop" ] && exit 0
# Through the host: replies; no private ranges; HTTPS out; DNS to two resolvers.
iptables -I DOCKER-USER 1 -s "$DEMO" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN $C
i=2
for dst in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  iptables -I DOCKER-USER $i -s "$DEMO" -d "$dst" -j DROP $C; i=$((i + 1))
done
iptables -I DOCKER-USER $i -s "$DEMO" -p tcp --dport 443 -j RETURN $C; i=$((i + 1))
for ns in 1.1.1.1 9.9.9.9; do              # the stack's own `dns:` resolvers
  for proto in udp tcp; do
    iptables -I DOCKER-USER $i -s "$DEMO" -d "$ns" -p $proto --dport 53 -j RETURN $C; i=$((i + 1))
  done
done
iptables -I DOCKER-USER $i -s "$DEMO" -j DROP $C
# To the host itself: replies only.
iptables -I INPUT 1 -s "$DEMO" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT $C
iptables -I INPUT 2 -s "$DEMO" -j DROP $C
```

Run it at boot after Docker starts, as root, from a root-owned file nobody else
can write to: whoever can edit it gets root.

## Monitoring

`GET /health` answers `{"status":"ok"}` without signing in. Point your uptime
monitor at it every few minutes, or let another Run·dere server watch it with
`TX_WATCH_URLS` (D-197).

## Upgrading

1. Bump the image tag to the same version as production and redeploy.
2. **Only if that release changed the sample data** (its release notes say so),
   re-seed once. This is the only step that wipes the demo's data:
   ```bash
   docker exec <demo container> python -m app.cli demo-seed --replace
   ```

## Day to day

- Nothing to run: the nightly roll keeps the trips current.
- Move the trips by hand: `docker exec <demo container> python -m app.cli demo-roll`.
- Start over: stop the stack, delete `demo.db` from the data volume, and start it
  again. It re-seeds.
- There's no demo admin by design (D-198); manage it from the command line.

## Privacy

Say what the demo keeps. demo.rundere.app's own policy is at
[rundere.app/privacy](https://rundere.app/privacy): no request log on the demo,
the proxy's own logs for a limited time to block abuse, and visit counts by country
and region without IP addresses.
