# Generic extension bootstrap (MPL-2.0)

`tempExtension.cfe` is the unmodified generic empty extension shipped by
[oscript-library/gitsync](https://github.com/oscript-library/gitsync), commit
`82d87f54942400362e3950d8190f9323bcb883c0`.

Source: `src/core/Классы/internal/bindata/Классы/tempExtension_Gitsync.os`,
function `ДвоичныеДанные`, decoded from its base64 literal. The embedded MD5
`6B3A3B869213E02BE3C63E74A4117049` was verified during extraction.
SHA256: `3d4816246e24aa41f4870ae70ac7cc3fdfcdce6e6faf637ac1fcb9af46217102`.
Size: 3720 bytes. Upstream license: Mozilla Public License 2.0 (see root LICENSE).
This is NOT an export of any acceptance or user repository.

Upstream `МенеджерСинхронизации.os:1308–1331` calls
`/LoadCfg <tempExtension.cfe> -Extension <requested name>` before initializing
repository access. The Python native backend follows the same bootstrap for
EVERY new, private worker infobase (including history and resumed runs).
It then reads actual repository versions using Report/DumpCfg with `-Extension`;
the template is never committed or used as version data. No attach/lock/commit
repository operations are required. A failed bootstrap is not cached and cannot
proceed to repository access. Each Designer invocation uses the configured timeout.

The template is included in the Python package via hatch's package directory.
No external bootstrap argument or stand-specific CFE path is required.
Programmatic `ib_factory` must return a private disposable IB: bootstrap loads
into that IB, so a production/shared IB must NEVER be returned by that factory.
Native repository sync supports one named extension per invocation, not a batch
of all extensions. Platform compatibility is bounded by actual native testing;
other platform versions are not implied by the presence of this resource.
