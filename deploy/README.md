# Deploying TeVarta on one host (Oracle Always Free, or any Linux VM)

One machine runs everything in Docker Compose: **Caddy** (HTTPS) → **API**, plus a
**Celery worker** and **beat**, **Redis**, **Postgres + pgvector**, and **Neo4j
Community**. The same files run on Oracle Ampere (arm64), AWS or Azure (amd64 or arm64).
Everything below marked *tested* was run in this repo's CI-equivalent setup on arm64;
everything marked *not tested* needs a real host.

## 0. Before you start

| You need | Notes |
|---|---|
| An Oracle Cloud account | Use a **US credit card** (not debit, prepaid or virtual). Pick a US **home region** and note it can't be changed. |
| A domain name for the API | e.g. `api.tevarta.com`. You can smoke-test on the bare IP first. |
| An `age` key pair | `age-keygen -o age.key`. Keep `age.key` **offline**; only the public key goes on the server. |
| A private object-storage bucket | For off-host backups (Oracle Object Storage, 20 GB free). |

## 1. Create the instance (Oracle)

1. Console → Compute → Instances → Create. Shape **VM.Standard.A1.Flex**, **2 OCPU / 12 GB**
   (the whole Always Free allowance), image **Ubuntu 24.04 (aarch64)**, boot volume 100 GB.
2. Add your SSH public key. Note the public IP.
3. If you get *"out of host capacity"*, retry later or in another availability domain. This
   is common for free Ampere shapes.
4. **Open the ports twice** (the classic Oracle trap):
   - Cloud side: VCN → Security List → ingress rules for TCP **80** and **443** from `0.0.0.0/0`.
   - Host side: Oracle's Ubuntu images ship with iptables rules that block them:
     ```sh
     sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
     sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
     sudo netfilter-persistent save
     ```
   Leave 22 (SSH) restricted to your own IP if you can.

## 2. Install Docker and tools

```sh
sudo apt-get update && sudo apt-get install -y ca-certificates curl git age rclone
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER   # log out and in
git clone https://github.com/Aaryan1524/TeVarta.git /opt/tevarta && cd /opt/tevarta
```

## 3. Secrets (never in git, never in chat)

```sh
sudo install -d -m 700 -o $USER /etc/tevarta /etc/tevarta/secrets
# Docker mounts these into containers whose users differ from yours, so the files are
# world-readable and the 700 directory above is what protects them on the host.
openssl rand -base64 32 | tr -d '\n' | sudo tee /etc/tevarta/secrets/postgres_password >/dev/null
printf 'neo4j/%s' "$(openssl rand -base64 32 | tr -d '/+=\n')" | sudo tee /etc/tevarta/secrets/neo4j_auth >/dev/null
sudo chmod 444 /etc/tevarta/secrets/*
```

Create `/etc/tevarta/tevarta.env` (`chmod 600`):

```
TEVARTA_ENV=production
TEVARTA_CREDENTIAL_SET=production
TEVARTA_POSTGRES_URL=postgresql://tevarta:<postgres_password>@postgres:5432/tevarta
TEVARTA_NEO4J_URL=bolt://neo4j:7687
TEVARTA_NEO4J_PASSWORD=<the part of neo4j_auth after "neo4j/">
TEVARTA_REDIS_URL=redis://redis:6379/0
TEVARTA_R2_BUCKET=tevarta-bodies
TEVARTA_TRUSTED_PROXIES=172.31.40.10
TEVARTA_HTTP_CONTACT=<your ops email>
TEVARTA_SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=<key>
OPENAI_API_KEY=<key>
TEVARTA_EMBEDDING_USD_PER_MTOK=<current price per million tokens>
TEVARTA_APPLE_CLIENT_ID=<ios bundle id>
TEVARTA_JWT_SIGNING_KEY=<32+ random characters>
TEVARTA_REFRESH_TTL_SECONDS=<founder-approved lifetime, seconds>
TEVARTA_REQUIRE_APPLE_NONCE=false
```

Two rules the app enforces (*tested*):
- With `TEVARTA_ENV=production`, no service URL or bucket name may contain `localhost`,
  `dev` or `development`, and `TEVARTA_CREDENTIAL_SET` must be `production`.
- If `TEVARTA_APPLE_CLIENT_ID`, `TEVARTA_JWT_SIGNING_KEY` or `TEVARTA_REFRESH_TTL_SECONDS`
  is missing, the API silently falls back to **fixture mode**. Verify auth is live (step 5).

## 4. Start it

```sh
cd /opt/tevarta/deploy
export TEVARTA_HOSTNAME=api.tevarta.com        # DNS A record -> the instance's public IP
docker compose -f compose.production.yaml build
docker compose -f compose.production.yaml up -d
docker compose -f compose.production.yaml ps     # everything healthy
```

Point the DNS record first; Caddy then obtains the certificate by itself. To smoke-test
before DNS exists, use `TEVARTA_HOSTNAME=:80` (HTTP only, so the API will redirect: expected).

## 5. Verify (*tested locally on arm64 with the same files*)

```sh
curl -s https://api.tevarta.com/health                                 # {"status":"ok"} + HSTS header
curl -s -o /dev/null -w "%{http_code}\n" https://api.tevarta.com/v1/feed                   # 401
curl -s -H 'Authorization: Bearer fixture-access-token' -w "%{http_code}\n" https://api.tevarta.com/v1/feed   # 401, NOT 200
```

The last must be 401. A 200 means auth is not configured and the API is serving fixtures.

## 6. Load the data (first deploy only, in this order)

```sh
cd /opt/tevarta/deploy
RUN="docker compose -f compose.production.yaml run --rm --no-deps -v /tmp:/tmp migrate"
# 6a. SEC catalog (needs your contact in the User-Agent):
curl -A "TeVarta/1.0 ($OPS_EMAIL)" -o /tmp/company_tickers.json https://www.sec.gov/files/company_tickers.json
$RUN python scripts/import_sec_catalog.py /tmp/company_tickers.json --database-url "$TEVARTA_POSTGRES_URL"
# 6b. Curated edges (only after Aaditya's evidence review is merged):
$RUN python scripts/curate_edges.py validate --database-url "$TEVARTA_POSTGRES_URL"
$RUN python scripts/curate_edges.py load --database-url "$TEVARTA_POSTGRES_URL" --neo4j-url bolt://neo4j:7687
# 6c. Pre-warm the 50 likely seeds:
$RUN python scripts/prewarm.py --database-url "$TEVARTA_POSTGRES_URL" --neo4j-url bolt://neo4j:7687 --no-search
```

The `--database-url` values are the DSN from your env file (Compose service names resolve
inside the stack). **Do this before turning on sources**: with an empty catalog every
article is tier 3. (That is what the local smoke run showed: 400 articles, all tier 3.)

## 7. Backups (do this before inviting anyone: MVP §20.1)

```sh
sudo install -d -m 700 /var/backups/tevarta
rclone config                      # create a remote for your private bucket
export BACKUP_AGE_RECIPIENT=age1...   # PUBLIC key only
export BACKUP_REMOTE=oracle:tevarta-backups
./backup.sh                        # then schedule it, e.g. cron: 0 3 * * * ...
```

`backup.sh` (*tested*): refuses to run without an encryption key, writes each file under a
temporary name and renames it only on success, restarts Neo4j even on failure, and leaves
a `last-success` timestamp to monitor. Neo4j is stopped for about a minute (Community
Edition can only dump offline), so the 03:00 schedule matters.

**Prove a restore at least once** (*tested*): `./restore-drill.sh BACKUP.dump.age age.key`
restores into a throwaway database, prints row counts next to the live database, and
drops it. Keep `age.key` offline.

## 8. Operating notes

- **Idle reclaim.** Oracle may reclaim an Always Free instance idle for 7 days (CPU,
  network *and* memory all under 20%). Run `./check-idle.sh` from cron; it warns below 25%
  memory. The databases hold data in RAM, so this should not trigger, but watch it.
- **Updates.** `git pull && docker compose -f compose.production.yaml build && docker compose -f compose.production.yaml up -d`.
  Migrations run automatically (the `migrate` service) before the API starts.
- **Costs.** Set hard spend limits on OpenAI and Brave. The search cap (1,500 queries a
  month) is enforced in code.
- **Not covered here:** monitoring and alerting, the R2/S3 body store (bodies currently
  live in the `bodies` Docker volume on this host), and APNs.
