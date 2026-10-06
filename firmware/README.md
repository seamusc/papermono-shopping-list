# Firmware

PlatformIO project for the M5Stack PaperMono.

```sh
cp src/secrets.example.h src/secrets.h   # Wi-Fi + server URL
pio run -t upload
```

Back up the device's flash before the first flash. See [docs/firmware.md](../docs/firmware.md)
for configuration, backup, flashing from a browser and troubleshooting, and
[docs/architecture.md](../docs/architecture.md#firmware-structure) for how the code is laid out.

To build without installing PlatformIO, run `just firmware` from the repo root: it builds in a container
(`firmware/builder/`) and writes `firmware/dist/<version>.bin`. Wi-Fi and server settings can be injected from the
environment (`SHOPPING_LIST_WIFI_SSID`, `SHOPPING_LIST_WIFI_PASSWORD`, `SHOPPING_LIST_SERVER_URL`) or with
`just firmware -c settings.env`, so no `secrets.h` is needed. See [docs/firmware.md](../docs/firmware.md#build).
