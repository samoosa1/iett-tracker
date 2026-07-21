#!/usr/bin/env bash
# STEP 0 — run this ON the candidate host (the VPS), BEFORE anything else.
# It checks whether İBB's API answers from that host's IP. İBB may only allow
# Turkish/regional IPs; if this host is blocked, pick a Turkey/EU host instead.
set -u
echo "This host's public IP: $(curl -s --max-time 10 ifconfig.me || echo '?')"
echo

probe() {  # url  soapaction  body  label  needle
  code=$(curl -s -o /tmp/ibb_resp -w '%{http_code}' --max-time 25 -X POST "$1" \
    -H "Content-Type: text/xml; charset=utf-8" -H "SOAPAction: $2" --data "$3" 2>/dev/null)
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    echo "  $4: UNREACHABLE (timeout / connection refused) → likely geo-blocked"
  elif grep -qi "$5" /tmp/ibb_resp; then
    echo "  $4: HTTP $code, real data ✓  (reachable AND working)"
  else
    echo "  $4: HTTP $code, no data (İBB service may just be down — but host CAN reach it)"
  fi
}

ENV='<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>%s</soap:Body></soap:Envelope>'

# The timetable endpoint is the reliable test (it's almost always up).
probe "https://api.ibb.gov.tr/iett/UlasimAnaVeri/PlanlananSeferSaati.asmx" \
  "http://tempuri.org/GetPlanlananSeferSaati_json" \
  "$(printf "$ENV" '<GetPlanlananSeferSaati_json xmlns="http://tempuri.org/"><HatKodu>522</HatKodu></GetPlanlananSeferSaati_json>')" \
  "timetable (reliability test)" "DT"

# Live positions (often down regardless — a 200/500 still means 'reachable').
probe "https://api.ibb.gov.tr/iett/FiloDurum/SeferGerceklesme.asmx" \
  "http://tempuri.org/GetHatOtoKonum_json" \
  "$(printf "$ENV" '<GetHatOtoKonum_json xmlns="http://tempuri.org/"><HatKodu>522</HatKodu></GetHatOtoKonum_json>')" \
  "live positions" "kapino"

echo
echo "VERDICT: if the timetable line says 'reachable' (any HTTP code, not UNREACHABLE),"
echo "this host is fine. If it says UNREACHABLE, use a Turkey or nearby-EU host instead."
