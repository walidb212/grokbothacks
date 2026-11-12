"""Live UK rates: SONIA from the Bank of England database (no key)."""
import requests
from engine.model import fig

BOE = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/2026"
       "&Dateto=now&SeriesCodes=IUDSOIA&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")


def sonia():
    try:
        r = requests.get(BOE, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        last = [l for l in r.text.strip().splitlines() if "," in l][-1]
        date, val = last.split(",")
        return fig("sonia", f"SONIA ({date.strip()})", round(float(val) / 100, 5), "%", "web",
                   ref="Bank of England IADB series IUDSOIA")
    except Exception:
        return fig("sonia", "SONIA (fallback)", 0.04, "%", "assumption")
