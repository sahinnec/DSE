# DSE Scout Auto

Roj DSE bondho hower por GitHub Actions data.json banay, `docs/index.html` seta pore.

## Setup (ekbar, ~10 minute)
1. github.com-e notun **public** repository banan (jemon `dse-scout`). Private-e free Pages hoy na.
2. Ei folder-er shob file upload korun (`.github`, `docs`, `scripts`, `requirements.txt`).
   Shob-er age `.github/workflows/update-data.yml` thik jaygay gelo ki na dekhen.
3. Settings -> Pages -> Source: **Deploy from a branch**, Branch: `main`, Folder: `/docs`.
4. Actions tab -> **Update DSE data** -> **Run workflow**. Shesh hole `docs/data.json` toiri hobe.
5. Link: `https://<apnar-username>.github.io/dse-scout/` (phone-e-o khulbe).

## Jodi Actions fail kore
- Log-e `403`/`timeout`/`ProxyError` thakle DSE hoyto GitHub-er (US) server block korche.
  Tokhon `scripts/fetch_data.py`-ke apnar nijer computer-e `python scripts/fetch_data.py` diye
  chalan (OUT=docs/data.json), tarpor file-ta repo-te upload korun.
- `docs/debug/company_*.json` dekhe `parse_fundamentals()`-er key-name milan.
  Financials parse na hole shob row "incomplete" thakbe. Eta ekta safe fail: plan dekhabe na.

## Ki auto, ki manual
Auto: close price, ATR14, support/stop/target, turnover, SMA50/200, circuit flag, EPS/NAV/dividend (best effort).
Manual (apni nijei): audit, khobor/PSI, bank-bima review, operating status, tick size ar lot (broker theke), financial latest kina.
