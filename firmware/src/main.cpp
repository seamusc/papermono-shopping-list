// PaperMono Shopping List - an e-paper shopping list that syncs with a small server on the home
// network. See docs/architecture.md for how the pieces fit together.
//
// Everything runs on the Arduino loop task. Each iteration: service the board (LED timeouts, power
// button, battery), route one touch event to whichever screen is in front, page on the side buttons,
// then run a sync if one is due. A sync blocks the loop while it runs.

#include <Arduino.h>
#include <M5Unified.h>

#include "config.h"
#include "hal/bsp.h"
#include "hal/epd.h"
#include "hal/touch.h"
#include "model.h"
#include "sync/local_store.h"
#include "sync/ota.h"
#include "sync/sync_client.h"
#include "ui/keyboard.h"
#include "ui/list_screen.h"
#include "ui/quantity_screen.h"
#include "ui/settings_screen.h"

using namespace ShoppingList;

// Overrides the Arduino core's weak default, which marks a new image valid the instant it boots. This
// keeps an OTA-installed image "pending verify" until the first successful sync calls
// Ota::confirmRunning(); if the device resets before then, the bootloader reverts to the previous
// image. C linkage because the core's version is defined in C.
extern "C" bool verifyRollbackLater() { return true; }

namespace {

ShoppingData g_data;
ListScreen g_list;

uint32_t g_nextSyncMs = 0;          // periodic sync due time
bool g_syncRequested = false;       // a local edit (or SYNC NOW) wants a sync as soon as possible
uint32_t g_syncCooldownUntilMs = 0; // set after a failed sync
uint32_t g_lastBatteryCheckMs = 0;

// Sleep bookkeeping (see maybeSleep()).
bool g_backgroundWake = false; // woken by the sync timer and nobody has touched anything since
bool g_quietSync = false;      // nothing touched since waking: a sync must not draw on the panel
bool g_sawInput = false;       // a touch or key press happened since the last wake
uint32_t g_lastActivityMs = 0; // last input, or the end of the last sync

constexpr uint32_t kBatteryCheckIntervalMs = 5000;
constexpr uint32_t kKeyDebounceMs = 40;

// Reports a physical button press once, after the reading has been stable for kKeyDebounceMs.
struct DebouncedKey {
    bool rawPrev = false;
    bool stable = false;
    uint32_t lastChangeMs = 0;

    bool pressed(bool raw, uint32_t now) {
        if (raw != rawPrev) {
            rawPrev = raw;
            lastChangeMs = now;
        }
        if (raw != stable && (now - lastChangeMs) >= kKeyDebounceMs) {
            stable = raw;
            return stable; // press edge only
        }
        return false;
    }
};
DebouncedKey g_key1;
DebouncedKey g_key2;

bool syncIsStale();

// Someone is (or was just) using the device, or a sync just ended: restart the idle clock. `input`
// marks a real touch or key press, which also ends the "quiet" mode where a sync leaves the panel alone.
void noteActivity(bool input) {
    g_lastActivityMs = millis();
    if (input) {
        g_sawInput = true;
        g_backgroundWake = false;
        g_quietSync = false;
    }
}

bool overlayOpen() {
    return KeyboardWidget::getInstance().isOpen() || SettingsScreen::getInstance().isOpen() ||
           QuantityScreen::getInstance().isOpen();
}

// Redraws the list with the high-quality waveform. Reserved for moments where content worth reading
// has changed (boot, a sync that changed something, closing an overlay); everything interactive
// uses partial updates instead.
void showListFull() {
    g_list.draw(M5.Display);
    Epd::getInstance().fullRefresh();
}

// Acts on a firmware update the last sync offered, if any. Runs after the sync so the list is already
// saved and on screen: a skipped or failed update never leaves it stale. On success the device
// reboots into the new image and this doesn't return.
void maybeUpdateFirmware() {
    const FirmwareOffer offer = SyncClient::getInstance().takeFirmwareOffer();
    if (!offer.valid()) return;

    const BatteryState battery = BSP::getInstance().getBatteryState();
    Ota& ota = Ota::getInstance();
    if (!ota.shouldInstall(offer, battery.percentage, battery.isCharging)) return;

    // Painted before the blocking download, like "syncing..." above.
    g_list.setUpdating(true);
    ota.install(offer);
    g_list.setUpdating(false);
}

// Delay until the next periodic sync. A successful sync carries the server's schedule; without one
// (failure, or an older server) fall back to the fixed interval.
uint32_t nextSyncDelayMs(bool syncOk) {
    const uint32_t scheduled = syncOk ? SyncClient::getInstance().nextSyncDelayMs() : 0;
    if (scheduled == 0) return Config::kSyncIntervalMs;
    if (scheduled < Config::kMinScheduledSyncMs) return Config::kMinScheduledSyncMs;
    if (scheduled > Config::kMaxScheduledSyncMs) return Config::kMaxScheduledSyncMs;
    return scheduled;
}

void runSync(uint32_t wifiTimeoutMs) {
    // Compare before and after so a periodic sync that changed nothing doesn't flash the panel.
    const std::vector<Category> previousCategories = g_data.categories;
    const std::vector<Item> previousItems = g_data.items;

    // A sync right after a wake runs with nobody touching anything. Leave the panel alone unless the
    // result changes what it should show: the panel keeps its image through sleep, and every refresh
    // costs power.
    const bool quiet = g_quietSync;
    const bool wasOk = SyncClient::getInstance().status().lastOk;

    // Painted immediately, before the blocking call below - a tap-triggered sync (see
    // Config::kTapSyncStaleMs) can otherwise take several seconds of Wi-Fi connect + HTTP with no
    // feedback, which looks like the tap did nothing.
    if (!quiet) g_list.setSyncing(true);
    const bool ok = SyncClient::getInstance().sync(g_data, wifiTimeoutMs);
    if (!quiet) g_list.setSyncing(false);
    bool changed = false;
    if (ok) {
        if (!quiet) BSP::getInstance().setLedSyncOk();
        g_syncCooldownUntilMs = 0;
        g_list.onDataChanged();
        changed = g_data.categories != previousCategories || g_data.items != previousItems;
        if (changed) showListFull();
        // Reaching the server is what proves a freshly installed firmware works.
        Ota::getInstance().confirmRunning();
        maybeUpdateFirmware();
    } else {
        if (!quiet) BSP::getInstance().setLedSyncFailed();
        g_syncCooldownUntilMs = millis() + Config::kFailedSyncCooldownMs;
    }
    // A quiet sync that flipped between working and failing changes what the header should say
    // ("Synced ..." / "Offline, last ..."), which is worth one header repaint.
    if (quiet && !changed && ok != wasOk) g_list.setSyncing(false);
    g_nextSyncMs = millis() + nextSyncDelayMs(ok);
    g_syncRequested = false;
    noteActivity(false);
}

void maybeSync() {
    // Never while an overlay is open: the keyboard holds pointers into g_data.catalog, which a sync
    // replaces (see keyboard.h), and yanking Settings away mid-read would be jarring anyway.
    if (overlayOpen()) return;

    const uint32_t now = millis();
    const bool periodicDue = (int32_t)(now - g_nextSyncMs) >= 0;
    const bool cooledDown = (int32_t)(now - g_syncCooldownUntilMs) >= 0;
    if ((g_syncRequested || periodicDue) && cooledDown) {
        // Someone just tapped something: fail fast if we're out of range.
        runSync(g_syncRequested ? Config::kInteractiveWifiTimeoutMs : Config::kWifiConnectTimeoutMs);
    }
}

void openSettings() {
    const SyncStatus& sync = SyncClient::getInstance().status();
    SettingsSnapshot snapshot;
    snapshot.wifiSsid = Config::kWifiSsid;
    snapshot.everSynced = sync.everAttempted;
    snapshot.lastSyncOk = sync.lastOk;
    snapshot.lastSyncAgoSec = sync.everAttempted ? (millis() - sync.lastAttemptMs) / 1000 : 0;
    snapshot.batteryPercent = BSP::getInstance().getBatteryState().percentage;
    snapshot.frontlightPercent = BSP::getInstance().getFrontlight();

    SettingsScreen::getInstance().open(
        snapshot, [](uint8_t percent) { BSP::getInstance().setFrontlight(percent); },
        []() {
            g_syncRequested = true;
            g_syncCooldownUntilMs = 0;
        });
}

void handleTouch() {
    TouchManager& touch = TouchManager::getInstance();
    if (!touch.hasEvent()) return;
    const TouchEvent ev = touch.popEvent();
    noteActivity(true);

    // Any tap is a sign someone's actively using the device - opportunistically sync if the last one
    // is getting stale, rather than leaving the list stale until the next scheduled sync, which can
    // be hours away. Safe to request unconditionally even while an overlay is open:
    // maybeSync() only actually runs a sync once overlayOpen() is false.
    if (ev.type == TouchEventType::Click && syncIsStale()) g_syncRequested = true;

    const bool wasOverlay = overlayOpen();
    if (KeyboardWidget::getInstance().isOpen()) KeyboardWidget::getInstance().handleTouch(ev);
    else if (SettingsScreen::getInstance().isOpen()) SettingsScreen::getInstance().handleTouch(ev);
    else if (QuantityScreen::getInstance().isOpen()) QuantityScreen::getInstance().handleTouch(ev);
    else g_list.handleTouch(ev);

    // An overlay stays painted on the panel until something repaints over it. Every way of closing
    // one (SEND, tap outside, swipe, CLOSE, SYNC NOW) ends up here, so repaint once, here.
    if (wasOverlay && !overlayOpen()) showListFull();
}

void handleKeys() {
    if (overlayOpen()) return;
    const uint32_t now = millis();
    if (g_key1.pressed(digitalRead(Pins::KEY1) == LOW, now)) {
        noteActivity(true);
        g_list.page(-1);
    }
    if (g_key2.pressed(digitalRead(Pins::KEY2) == LOW, now)) {
        noteActivity(true);
        g_list.page(1);
    }
}

void checkBattery() {
    const uint32_t now = millis();
    if (now - g_lastBatteryCheckMs < kBatteryCheckIntervalMs) return;
    g_lastBatteryCheckMs = now;

    const BatteryState state = BSP::getInstance().getBatteryState();
    if (!state.isCharging && state.voltageMv <= BSP::LOW_BATTERY_CUTOFF_MV) {
        BSP::getInstance().lowBatteryShutdown(); // does not return
    }
}

// The list is stale enough that a tap (or a wake) should sync it.
bool syncIsStale() {
    const SyncStatus& status = SyncClient::getInstance().status();
    return status.lastSuccessMs == 0 ||
           (int32_t)(millis() - status.lastSuccessMs) >= (int32_t)Config::kTapSyncStaleMs;
}

void closeOverlays() {
    KeyboardWidget::getInstance().close();
    SettingsScreen::getInstance().close();
    QuantityScreen::getInstance().close();
    showListFull();
}

// Back from a sleep: work out what to do next from what woke us.
void onWake(WakeReason reason) {
    g_lastActivityMs = millis();
    g_sawInput = false;
    g_quietSync = true; // stays quiet until something is actually touched
    // The key that woke us may still be down. Don't read that as a page turn.
    g_key1.rawPrev = g_key1.stable = digitalRead(Pins::KEY1) == LOW;
    g_key2.rawPrev = g_key2.stable = digitalRead(Pins::KEY2) == LOW;

    if (reason == WakeReason::Timer) {
        // The timer was set to the moment the sync falls due, so sync now without consulting the clock.
        g_backgroundWake = true;
        g_nextSyncMs = millis();
    } else {
        // A person woke it and the list may be old: refresh it the way a tap on a stale list does.
        g_backgroundWake = false;
        if (syncIsStale()) g_syncRequested = true;
    }
}

// Light-sleep whenever there is nothing to do. The panel keeps its image and the chip keeps its RAM, so
// the device wakes exactly as it was. It wakes for the next scheduled sync, or for a key or touch.
void maybeSleep() {
    if (!Config::kSleepEnabled) return;
    const uint32_t now = millis();

    if (g_syncRequested) return;
    if (!Config::kSleepWhileCharging && BSP::getInstance().getBatteryState().isCharging) return;

    // An overlay left open would keep the device awake indefinitely; close it after a while.
    if (overlayOpen()) {
        if (now - g_lastActivityMs >= Config::kOverlayIdleCloseMs) {
            closeOverlays();
            noteActivity(false);
        }
        return;
    }

    const uint32_t idleNeededMs = g_backgroundWake ? Config::kBackgroundSettleMs
                                  : g_sawInput     ? Config::kInteractiveIdleSleepMs
                                                   : Config::kNoInputWakeSleepMs;
    if (now - g_lastActivityMs < idleNeededMs) return;

    // A sync that's due (or about to be) runs first; maybeSync() has already had its turn this loop.
    const int32_t untilSyncMs = (int32_t)(g_nextSyncMs - now);
    if (untilSyncMs < (int32_t)Config::kMinSleepMs) return;

    onWake(BSP::getInstance().lightSleep((uint32_t)untilSyncMs));
}

} // namespace

void setup() {
    Serial.begin(115200);
    delay(100);
    Serial.println("PaperMono Shopping List booting...");

    BSP::getInstance().init();
    Epd::getInstance().init();
    TouchManager::getInstance().init();
    LocalStore::getInstance().init();
    Ota::getInstance().begin();

    // Show whatever was cached last time straight away; the first sync runs on the first loop.
    LocalStore::getInstance().loadCache(g_data);
    g_list.bind(&g_data);
    g_list.setSyncRequestCallback([]() { g_syncRequested = true; });
    g_list.setOpenSettingsCallback(openSettings);
    showListFull();

    g_nextSyncMs = millis();
}

void loop() {
    BSP::getInstance().update();
    TouchManager::getInstance().update();
    checkBattery();

    if (BSP::getInstance().checkPowerButton()) BSP::getInstance().powerOff();

    handleTouch();
    handleKeys();
    maybeSync();
    maybeSleep();

    delay(20);
}
