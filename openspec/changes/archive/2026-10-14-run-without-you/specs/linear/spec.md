# Delta: linear

## ADDED Requirements

### Requirement: Tickets become plan input, not work
A Linear ticket SHALL only be read when you run `orchestra plan --linear <ID>` or
`orchestra linear show <ID>`; the plan still waits for your approval.
