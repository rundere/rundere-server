# iOS app contract snapshot

The iOS / iPadOS app now lives in its own repository, **rundere/rundere-ios**
(private). `DTOs.swift` here is a copy of that app's `Rundere/Models/DTOs.swift`:
`tests/test_api_contract.py` runs `scripts/dto_drift.py` against it and the live
`/api/v1` schema, so a server change that would break the app's decoding fails CI.

Don't edit it here. When the app's DTOs change, refresh it from the app repo:

```bash
scripts/sync-contract.sh dtos ../rundere-server    # run in rundere-ios
```
