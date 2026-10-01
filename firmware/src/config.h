#pragma once

// Build-time configuration. Per-install values (Wi-Fi, server address) live in secrets.h, which is
// git-ignored: copy secrets.example.h to secrets.h and fill it in before building.

#include <cstdint>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing firmware/src/secrets.h - copy secrets.example.h to secrets.h and fill in your Wi-Fi and server details."
#endif

namespace ShoppingList {
namespace Config {

constexpr const char* kWifiSsid = SHOPPING_LIST_WIFI_SSID;
constexpr const char* kWifiPassword = SHOPPING_LIST_WIFI_PASSWORD;
constexpr const char* kServerBaseUrl = SHOPPING_LIST_SERVER_URL;

#ifndef FW_VERSION
#error "FW_VERSION isn't defined - it's set in platformio.ini's build_flags."
#endif
constexpr const char* kFirmwareVersion = FW_VERSION;

// Wi-Fi is only switched on for the length of one sync. A periodic sync can afford to wait; one
// triggered by a tap uses the shorter timeout, because the main loop (touch included) blocks for the
// whole connect attempt and the common real-world failure is being out of range in the shop.
constexpr uint32_t kWifiConnectTimeoutMs = 15000;
constexpr uint32_t kInteractiveWifiTimeoutMs = 5000;
constexpr uint32_t kHttpTimeoutMs = 8000;

// How long to wait for the next periodic sync when the server hasn't said (a failed sync, or an older
// server). Normally the server's schedule decides: each sync response carries `next_sync_in_s`.
constexpr uint32_t kSyncIntervalMs = 60UL * 60UL * 1000UL;
// Whatever the server asks for is clamped to this range, so a bad value can neither spin the radio
// nor leave the device out of touch for good.
constexpr uint32_t kMinScheduledSyncMs = 30UL * 1000UL;
constexpr uint32_t kMaxScheduledSyncMs = 12UL * 60UL * 60UL * 1000UL;
// After a failed sync, don't retry for this long, so repeated taps don't each pay the full timeout.
constexpr uint32_t kFailedSyncCooldownMs = 30UL * 1000UL;
// A tap anywhere on the shopping list opportunistically syncs if the last one is older than this -
// otherwise, with periodic syncs up to hours apart (overnight, or on a sparse schedule), a stale list
// would just sit there until the next one while someone's actively using the device.
constexpr uint32_t kTapSyncStaleMs = 5UL * 60UL * 1000UL;

// Force a full-panel refresh after this many fast partial updates. Unbounded partial refreshes
// build up ghosting and DC imbalance on the SSD1677 panel; ~10 is the usual guidance for this
// hardware.
constexpr uint16_t kMaxPartialRefreshes = 10;

// OTA updates (see sync/ota.h). Only started with this much battery left, or on charge, so a flash
// can't run out of power halfway.
constexpr int kOtaMinBatteryPercent = 50;
// After a failed attempt, don't try again for this long, so a bad link can't turn every sync into a
// download-and-fail loop.
constexpr uint32_t kOtaRetryAfterFailureMs = 24UL * 60UL * 60UL * 1000UL;
// Give up on a download that hasn't delivered a byte for this long.
constexpr uint32_t kOtaStallTimeoutMs = 15000;

// --- Sleep ------------------------------------------------------------------------------------------
// Between syncs the device light-sleeps: the CPU stops and the radio is off, but RAM, the panel and the
// touch controller keep their state, so it wakes with the list still on the glass and a tap is not lost.
// (Deep sleep was tried first; it reboots the chip, and the panel driver came back blank. M5Stack's own
// guide and the best-known PaperMono firmware also use light sleep for normal operation.) Build with
// -D SHOPPING_LIST_SLEEP=0 to keep the device awake, e.g. when debugging over serial.
#ifndef SHOPPING_LIST_SLEEP
#define SHOPPING_LIST_SLEEP 1
#endif
constexpr bool kSleepEnabled = SHOPPING_LIST_SLEEP != 0;
// Sleep on USB power too, so what you test is what you ship. (Light sleep drops the USB serial port
// from the host while it lasts.)
constexpr bool kSleepWhileCharging = true;
// Awake time with no input before sleeping, after a wake someone caused (a key press or a touch).
constexpr uint32_t kInteractiveIdleSleepMs = 30UL * 1000UL;
// A touch or key woke the device but nothing has been pressed since: it was probably a glance or a
// false alarm, so don't stay up for the full idle time.
constexpr uint32_t kNoInputWakeSleepMs = 8UL * 1000UL;
// A timer wake that only syncs: sleep again this long after the sync (or the failed attempt) ends.
constexpr uint32_t kBackgroundSettleMs = 1500;
// An overlay (keyboard, settings, quantity) left open this long is closed, so it can't keep the
// device awake.
constexpr uint32_t kOverlayIdleCloseMs = 2UL * 60UL * 1000UL;
// Don't bother sleeping for less than this; the wake would cost more than staying up.
constexpr uint32_t kMinSleepMs = 3000;

// Upper bound on queued offline edits; normal use keeps 0-3.
constexpr size_t kMaxPendingActions = 100;

} // namespace Config
} // namespace ShoppingList
