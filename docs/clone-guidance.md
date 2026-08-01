# Clone guidance

For a fast, space-efficient checkout of rebar, use Git's blobless filter:

```sh
git clone --filter=blob:none https://github.com/navapbc/rebar.git
```

Measured on this repository, a full clone took **1.04 GiB / 59 s**. A blobless
clone took **68.7 MiB / 14.3 s**. Do **not** use `--filter=tree:0` here: it
measured **3.57 GiB**, 3.4x larger than the full clone.

Blobless clones have one accepted degradation: rebar's best-effort automatic
`caused_by` inference runs `git blame` against historical file versions when a
bug closes without an explicit `--caused-by`. Git may fetch those missing blobs
one at a time on the first such close. If blame cannot read every impacted path,
rebar records no derived `caused_by` link rather than guessing from a partial
result. Supply `--caused-by` when an explicit link is preferable.

rebar cannot filter a clone it does not perform; choose the filter when running
`git clone` (or configure the tooling that creates the checkout).
