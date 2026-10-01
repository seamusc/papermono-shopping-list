#include "sync/sync_client.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_log.h>
#include <esp_random.h>

#include "config.h"
#include "hal/bsp.h"
#include "sync/local_store.h"
#include "sync/model_json.h"

static constexpr const char* TAG = "Sync";

namespace ShoppingList {

bool SyncClient::connectWifi(uint32_t timeoutMs) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(Config::kWifiSsid, Config::kWifiPassword);

    const uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > timeoutMs) {
            ESP_LOGW(TAG, "Wi-Fi connect timed out after %u ms", (unsigned)timeoutMs);
            WiFi.mode(WIFI_OFF);
            return false;
        }
        delay(100);
    }
    ESP_LOGI(TAG, "Wi-Fi connected, IP %s", WiFi.localIP().toString().c_str());
    return true;
}

void SyncClient::disconnectWifi() {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
}

SyncClient::PushResult SyncClient::push(const PendingAction& action) {
    JsonDocument body;
    String method;
    String path;
    if (action.type == PendingActionType::AddItem) {
        method = "POST";
        path = "/api/items";
        body["name"] = action.name;
        if (action.categoryId != kNoCategory) body["category_id"] = action.categoryId;
        if (action.hasQuantity) {
            body["quantity"] = action.quantity;
            if (action.unit.length() > 0) body["unit"] = action.unit;
        }
    } else if (action.type == PendingActionType::SetQuantity) {
        method = "PATCH";
        path = "/api/items/" + String(action.itemId);
        // Unlike TogglePurchased, null is a real target value here (it means "clear the
        // quantity") - see the server's ItemUpdate/`model_fields_set` handling.
        if (action.hasQuantity) {
            body["quantity"] = action.quantity;
            body["unit"] = action.unit.length() > 0 ? action.unit : (const char*)nullptr;
        } else {
            body["quantity"] = (const char*)nullptr;
            body["unit"] = (const char*)nullptr;
        }
    } else {
        method = "PATCH";
        path = "/api/items/" + String(action.itemId);
        body["purchased"] = action.purchased;
    }
    String payload;
    serializeJson(body, payload);

    HTTPClient http;
    http.setTimeout(Config::kHttpTimeoutMs);
    if (!http.begin(String(Config::kServerBaseUrl) + path)) return PushResult::Retry;
    http.addHeader("Content-Type", "application/json");
    // sendRequest() rather than PATCH(): the convenience method only exists in some ESP32 core versions.
    const int code = http.sendRequest(method.c_str(), payload);
    http.end();

    if (code >= 200 && code < 300) return PushResult::Done;
    const PushResult result = (code >= 400 && code < 500) ? PushResult::Drop : PushResult::Retry;
    ESP_LOGW(TAG, "%s %s failed (%d) - %s", method.c_str(), path.c_str(), code,
             result == PushResult::Drop ? "server rejected it, dropping" : "will retry");
    return result;
}

void SyncClient::replayPending() {
    LocalStore& store = LocalStore::getInstance();
    const std::vector<PendingAction> pending = store.loadPending();
    if (pending.empty()) return;

    std::vector<PendingAction> retry;
    for (const auto& action : pending) {
        if (push(action) == PushResult::Retry) retry.push_back(action);
    }
    store.replacePending(retry);
    ESP_LOGI(TAG, "Replayed queued edits: %u sent or dropped, %u still queued",
             (unsigned)(pending.size() - retry.size()), (unsigned)retry.size());
}

// Device health and trace context for the server's telemetry (see docs/server.md#observability-
// opentelemetry). Purely informational: the server never changes its answer because of them, and
// implausible readings are left out rather than sent.
static void addHealthHeaders(HTTPClient& http) {
    const int percent = BSP::getInstance().getBatteryState().percentage;
    if (percent >= 0 && percent <= 100) http.addHeader("X-Battery-Percent", String(percent));
    const int rssi = WiFi.RSSI();
    if (rssi < 0) http.addHeader("X-Wifi-Rssi", String(rssi));
    http.addHeader("X-Free-Heap", String((unsigned)ESP.getFreeHeap()));
    http.addHeader("X-Power", BSP::powerStats());

    // W3C traceparent, so the server's spans and logs for this sync share a trace id we can also
    // print here and match against serial output.
    char traceparent[56];
    snprintf(traceparent, sizeof traceparent, "00-%08x%08x%08x%08x-%08x%08x-01", (unsigned)esp_random(),
             (unsigned)esp_random(), (unsigned)esp_random(), (unsigned)esp_random(), (unsigned)esp_random(),
             (unsigned)esp_random());
    http.addHeader("traceparent", traceparent);
    ESP_LOGI(TAG, "sync traceparent %s", traceparent);
}

bool SyncClient::fetch(ShoppingData& data) {
    _nextSyncDelayMs = 0;
    HTTPClient http;
    http.setTimeout(Config::kHttpTimeoutMs);
    if (!http.begin(String(Config::kServerBaseUrl) + "/api/sync")) return false;
    // Lets the server decide whether to offer this device a firmware update.
    http.addHeader("X-Firmware-Version", Config::kFirmwareVersion);
    addHealthHeaders(http);
    // Parsing straight from the stream needs a length-delimited body, not chunked encoding; this is
    // ArduinoJson's documented way to get that from HTTPClient.
    http.useHTTP10(true);

    const int code = http.GET();
    if (code != 200) {
        ESP_LOGW(TAG, "GET /api/sync failed (%d)", code);
        http.end();
        return false;
    }

    JsonDocument doc;
    const DeserializationError err = deserializeJson(doc, http.getStream());
    http.end();
    if (err) {
        ESP_LOGE(TAG, "Failed to parse /api/sync response: %s", err.c_str());
        return false;
    }

    data.categories = ModelJson::readArray(doc["categories"].as<JsonArrayConst>(), ModelJson::categoryFrom);
    data.items = ModelJson::readArray(doc["items"].as<JsonArrayConst>(), ModelJson::itemFrom);
    data.catalog = ModelJson::readArray(doc["catalog"].as<JsonArrayConst>(), ModelJson::catalogEntryFrom);

    const char* syncedAt = doc["synced_at"] | "";
    _status.lastSyncClock = syncedAt;

    // The server's schedule (quiet hours, weekend profile) reaches us only as this one number.
    const uint32_t nextSyncSec = doc["next_sync_in_s"] | 0u;
    _nextSyncDelayMs = nextSyncSec <= UINT32_MAX / 1000 ? nextSyncSec * 1000 : 0;

    // "firmware" is absent unless the server wants this device on a different version.
    _offer = FirmwareOffer();
    const JsonObjectConst fw = doc["firmware"].as<JsonObjectConst>();
    if (!fw.isNull()) {
        _offer.version = fw["version"] | "";
        _offer.url = fw["url"] | "";
        _offer.sha256 = fw["sha256"] | "";
        _offer.size = fw["size"] | 0;
        if (!_offer.valid()) {
            ESP_LOGW(TAG, "ignoring malformed firmware offer");
            _offer = FirmwareOffer();
        }
    }
    return true;
}

bool SyncClient::sync(ShoppingData& data, uint32_t wifiTimeoutMs) {
    _status.everAttempted = true;
    _status.lastAttemptMs = millis();

    bool ok = false;
    _status.lastReachedWifi = false;
    if (connectWifi(wifiTimeoutMs)) {
        _status.lastReachedWifi = true;
        // Push before pulling, or a checkbox ticked just before this sync would be overwritten by
        // the snapshot that follows.
        replayPending();
        ok = fetch(data);
        disconnectWifi();
    }

    _status.lastOk = ok;
    if (!ok) return false;
    _status.lastSuccessMs = millis();
    BSP::resetPowerStats();

    // Adds that are still queued (their push failed transiently) aren't in the snapshot yet. Put them
    // back as local placeholders so they don't vanish from the screen - and, since this is what gets
    // cached, so they survive a reboot too.
    LocalStore& store = LocalStore::getInstance();
    for (const auto& action : store.loadPending()) {
        if (action.type != PendingActionType::AddItem) continue;
        Item placeholder;
        placeholder.name = action.name;
        placeholder.categoryId = action.categoryId;
        placeholder.hasQuantity = action.hasQuantity;
        placeholder.quantity = action.quantity;
        placeholder.unit = action.unit;
        data.items.push_back(placeholder);
    }
    store.saveCache(data);
    return true;
}

} // namespace ShoppingList
