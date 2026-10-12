# Runbook: Power outage (cell site or aggregation router)

**Symptoms:** MAINS_POWER_FAILURE, then BATTERY_LOW 20-40 min later, then the site drops (NG_LINK_FAILURE plus all cells out of service). The mains alarm can be lost; a BATTERY_LOW followed by a full-site outage is still a strong signal on its own.

**Diagnosis:**
1. Cell site: only one site affected; neighbours under the same aggregation router are healthy.
2. Aggregation router: if the router itself loses power, every site behind it drops at once. That looks like a fibre cut, but the router's own power/battery alarms come first.
3. Battery alarm precedes the outage.

**Actions (need NOC approval):**
- Contact the power utility / landlord for the site address.
- Dispatch a field engineer with a mobile generator.
- Raise neighbour cells' coverage (tilt/power) temporarily if the change policy allows.
