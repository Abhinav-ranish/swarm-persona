# Security

swarm-persona drives real browsers against real apps with real test sessions, so a hole in the
write guard (`src/swarm_persona/guard/`) is a security bug: any way a persona's browser can make a
request the policy should refuse, reach an internal address, or get around the proxy.

Report one privately through GitHub's "Report a vulnerability" button on this repository rather
than in a public issue. Include the request (method, URL, headers, body) and the policy it got
past. You will get an answer within a week.
