# Protocol, source and license provenance

This source release is licensed under **AGPL-3.0-only**. The maintainer
confirmed that the implementation is personal work. The original contributor
MIT notice and the ha-bambulab MIT notice are preserved under `LICENSES/`.

| Material | Provenance and release treatment |
| --- | --- |
| Bridge server, browser app and Home Assistant integration | Maintainer-owned implementation, distributed here under AGPL-3.0-only. |
| HMS/stage decoder and compatibility lookup | Restored in 0.1.2 from the deployed implementation. Source documentation cited [Bambu Studio](https://github.com/bambulab/BambuStudio) (AGPL-3.0) and [ha-bambulab](https://github.com/greghesp/ha-bambulab) (MIT). These references motivated the AGPL distribution choice; no MIT-only claim is made for the combined release. |
| Protocol identifiers and message layouts | Interoperability references included OpenBambuAPI, ha-bambulab and bambulabs_api. The bridge implements local MQTT/TLS, FTPS and camera interfaces using ordinary Python libraries. It does not import those applications. |
| Printer-generated files | Supplied locally by the operator at runtime. Real captures, camera photos, private certificates, 3DBenchy models and sliced jobs are excluded from this release. |
| Dependencies | Installed from the package lock and retain their respective licenses. Development tools are separate from the shipped application. |

The restored HMS module contains a selected compatibility table and raw-code
fallback. Its source notes identify ha-bambulab's stage/severity/module constants,
English HMS text and wiki links, accessed during development in May and June 2026.
The ha-bambulab MIT copyright and permission notice is retained in
LICENSES/ha-bambulab-MIT.txt; its [upstream license](https://github.com/greghesp/ha-bambulab/blob/main/LICENSE)
was rechecked for this release. Earlier Bambu Studio references remain acknowledged
under the combined AGPL license. No claim is made that manual transcription removes
upstream copyright obligations.

The exact historical upstream revisions of the compatibility tables have not been
established. The original five legacy records did not match the community catalog
checked during the first public-release review. Meanings and stage mappings still
require firmware-specific validation; unknown codes remain visible. Context notes
are advisory and do not establish that a physical print completed. Consult the
printer display before taking corrective action.

No proprietary Bambu networking plugin, firmware image, vendor cloud credential,
or official cloud-client identity metadata is bundled. Bambu's [published policy](https://blog.bambulab.com/setting-the-record-straight-on-cloud-access-and-community/)
distinguishes AGPL source distribution and LAN/developer workflows from cloud
client impersonation. This review does not establish patent clearance or a right
to use vendor cloud services or trademarks beyond identifying compatibility.

## Source access for users of the service

The browser app and viewer offer a source link to this release. If you distribute
or run a modified covered version for network users, provide its corresponding
source as required by AGPL section 13, retain notices, and update the source link
to the code actually deployed. Linking only to the unchanged upstream tree does
not provide source for your modifications. Full terms are in `LICENSE`.
