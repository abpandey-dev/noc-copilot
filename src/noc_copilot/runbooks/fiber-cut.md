# Runbook: Transport fibre cut / aggregation uplink down

**Symptoms:** LINK_DOWN on an aggregation router uplink; NG_LINK_FAILURE on several sites behind the same router; cells out of service across that area. The router's own alarm may be missing if the router lost its management path too.

**Diagnosis:**
1. Group site/cell outages by aggregation router. Three or more sites down behind one router points to transport, not the sites.
2. Confirm sites in other regions are healthy (rules out core).
3. Check whether outages started within the same 1-2 minutes.

**Actions (need NOC approval):**
- Dispatch a fibre field team to the uplink route; request an OTDR trace to locate the break.
- Activate the protection path / reroute over microwave backup if available.
- Raise a major-incident ticket and inform customer care for the affected area.
