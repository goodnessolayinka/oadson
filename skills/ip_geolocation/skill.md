# IP Geolocation

## What It Does
Returns geographic and network information for any IPv4 or IPv6 address. Includes country, city, coordinates, ISP, ASN, and organization details.

## When to Use It
- User says "where is this IP", "geolocate", "what ISP is this IP on"
- User doing network recon or threat investigation
- User tracking origin of suspicious traffic
- User verifying VPN or proxy exit node location

## When NOT to Use It
- User wants full device info on the IP → use shodan_search
- User wants domain resolution → use dns_lookup
- User wants to check their own IP — can answer directly

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `lookup` | one IP's location. |
| `bulk_lookup` | multiple IPs at once. |
| `my_ip` | the server's own current public IP/location. |

## Output Format
```json
{
  "ip": "8.8.8.8",
  "country": "United States",
  "country_code": "US",
  "city": "Mountain View",
  "region": "California",
  "coordinates": {"lat": 37.4056, "lon": -122.0775},
  "isp": "Google LLC",
  "organization": "Google Public DNS",
  "asn": "AS15169",
  "is_vpn": false,
  "is_proxy": false
}
```

## Constraints
- Accuracy: country-level high, city-level moderate (±50km)
- VPN/proxy detection is probabilistic
- Private IPs (192.168.x.x, 10.x.x.x) return no geolocation
- Data sourced from MaxMind/ipinfo — may lag on new allocations

## Examples
- "Where is the server at 196.216.2.1?"
- "Is this Nigerian IP on a known cloud provider?"
- "What ASN does Render use?"
