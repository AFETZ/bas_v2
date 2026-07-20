# Network Test Scaffolding

`check_no_bypass.sh` is the Day 1 no-bypass smoke check. It reads
`network/config/endpoints.yaml` and fails if a configured forbidden direct
localhost endpoint is reachable or bound without ns-3.

This is not the final P0 isolation proof. The final proof must run with active
ground and UAV endpoint namespaces, ns-3 stopped, and packet capture showing
that ground-side traffic cannot reach UAV-side SITL or HitL endpoints.

Run the complete Python validation/adversarial suite from the repository root:

```bash
python3 -m unittest discover -v network/tests 'test_*.py'
```

The current test count is reported in `network/PROGRESS.md`; a passing unit
suite strengthens M0 but never closes a runtime milestone by itself.
