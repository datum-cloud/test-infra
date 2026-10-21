"""Source-bound operator Flux journals; no generic object ownership exceptions."""
from apps_native_support import FluxOwned, require


def owner(cluster, key, contract, evidence, path, persist):
    require(isinstance(contract, dict) and set(contract) == {"manifest", "rendered", "revision"},
            "verified operator Flux source contract required")
    manifest = contract["manifest"]
    def record(saved):
        evidence.setdefault("flux_stages", {})[key] = saved
        evidence["attempted"][key] = saved["uid"]
        persist(path, evidence)
    meta = manifest["metadata"]
    return FluxOwned(cluster, meta["namespace"], meta["name"],
                     contract["rendered"], contract["revision"], record)


def create_stage(cluster, key, manifest, contracts, evidence, path, persist):
    require(key not in evidence["attempted"], "duplicate operator Flux create intent")
    contract = (contracts or {}).get(key)
    require(contract is not None and manifest == contract["manifest"],
            "operator Flux create differs from verified source")
    ref = owner(cluster, key, contract, evidence, path, persist)
    return ref.create(manifest, evidence["run_id"])


def delete_stage(cluster, proxy, key, contracts, evidence, path, persist):
    if key not in evidence["attempted"]:
        return
    contract = (contracts or {}).get(key)
    ref = owner(cluster, key, contract, evidence, path, persist)
    saved = evidence.get("flux_stages", {}).get(key)
    require(isinstance(saved, dict) and saved.get("uid") == evidence["attempted"][key],
            "missing or inconsistent operator Flux recovery journal; retain stage")
    ref.recover(contract["manifest"], evidence["run_id"], saved)
    ref.delete(proxy, evidence["run_id"])
