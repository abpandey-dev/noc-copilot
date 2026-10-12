# Runbook: Cell site power outage

**Symptoms:** MAINS_POWER_FAILURE, then BATTERY_LOW 20-40 min later, then the site drops (NG_LINK_FAILURE plus all cells out of service). The mains alarm can be lost; a BATTERY_LOW followed by a full-site outage is still a strong signal on its own.

**Diagnosis:**
1. Only one site affected; neighbours under the same aggregation router are healthy.
2. Battery alarm precedes the outage.

**Actions (need NOC approval):**
- Contact the power utility / landlord for the site address.
- Dispatch a field engineer with a mobile generator.
- Raise neighbour cells' coverage (tilt/power) temporarily if the change policy allows.
