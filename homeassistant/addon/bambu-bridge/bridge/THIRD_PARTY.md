# Protocol, source and license provenance

This source release is licensed under **AGPL-3.0-only**. The maintainer
confirmed that the implementation is personal work. The original contributor
MIT notice and the ha-bambulab MIT notice are preserved under `LICENSES/`.

| Material | Provenance and release treatment |
| --- | --- |
| Bridge server, browser app and Home Assistant integration | Maintainer-owned implementation, distributed here under AGPL-3.0-only. |
| Small HMS compatibility lookup | Earlier source documentation cited [Bambu Studio](https://github.com/bambulab/BambuStudio) (AGPL-3.0) and [ha-bambulab](https://github.com/greghesp/ha-bambulab) (MIT). These references motivated the AGPL distribution choice; no MIT-only claim is made for the combined release. |
| Protocol identifiers and message layouts | Interoperability references included OpenBambuAPI, ha-bambulab and bambulabs_api. The bridge implements local MQTT/TLS, FTPS and camera interfaces using ordinary Python libraries. It does not import those applications. |
| Printer-generated files | Supplied locally by the operator at runtime. Real captures, camera photos, private certificates, 3DBenchy models and sliced jobs are excluded from this release. |
| Dependencies | Installed from the package lock and retain their respective licenses. Development tools are separate from the shipped application. |

The five HMS records are a limited compatibility table, not an authoritative
vendor catalog. Their exact keys were absent from the current community catalog
checked during this review; the precise original upstream revision was not
established. Their meanings still need firmware-specific hardware validation.
Unknown codes remain visible in the API. Consult the printer's own display
before treating a message as a diagnosis or taking corrective action.

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
