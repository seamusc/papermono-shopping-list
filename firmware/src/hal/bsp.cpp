#include "hal/bsp.h"

#include <esp_log.h>
#include <esp_sleep.h>
#include <esp_timer.h>
#include <driver/gpio.h>
#include <driver/rtc_io.h>

static constexpr const char* TAG = "BSP";

// Shared PM1/IOE1 bus speeds: Fast Mode is attempted first, 100 kHz is the guaranteed fallback.
static constexpr uint32_t I2C_FREQ_FAST = 400000;
static constexpr uint32_t I2C_FREQ_SAFE = 100000;

namespace ShoppingList {

namespace {

// Power history for BSP::powerStats(). Plain RAM: light sleep keeps it.
uint16_t s_wakes[5];       // indexed by WakeReason
int64_t s_sleptMs = 0;     // total time spent in light sleep
uint32_t s_statsBaseMs = 0;  // millis() when the stats were last cleared
int64_t s_sleptAtBaseMs = 0;

} // namespace

String BSP::powerStats() {
    const int64_t slept = s_sleptMs - s_sleptAtBaseMs;
    int64_t awake = (int64_t)(millis() - s_statsBaseMs) - slept;
    if (awake < 0) awake = 0;
    char buf[112];
    snprintf(buf, sizeof(buf), "timer=%u;touch=%u;key=%u;pb=%u;other=%u;awake_ms=%lu;slept_ms=%lu", s_wakes[0],
             s_wakes[1], s_wakes[2], s_wakes[3], s_wakes[4], (unsigned long)awake, (unsigned long)slept);
    return String(buf);
}

void BSP::resetPowerStats() {
    memset(s_wakes, 0, sizeof(s_wakes));
    s_statsBaseMs = millis();
    s_sleptAtBaseMs = s_sleptMs;
}

WakeReason BSP::lightSleep(uint32_t sleepMs) {
    // Never sleep with a refresh running: the panel needs the CPU to finish it.
    M5.Display.waitDisplay();

    if (_currentBrightness > 0) _brightnessBeforeSleep = _currentBrightness;
    setFrontlight(0);
    setLed(false, 0, 0);

    // The PM1 holds its IRQ line low until its flags are cleared; left set, GPIO 1 would wake us at once.
    if (_pm1Ready) {
        _pm1.irqClearGpioAll();
        _pm1.irqClearSysAll();
        uint8_t btn = 0;
        _pm1.irqGetBtnStatus(&btn, M5PM1_CLEAN_ALL);
        bool flag = false;
        _pm1.btnGetFlag(&flag);
    }

    // The side keys and the PM1 IRQ line (power button) wake by GPIO level. The touch interrupt is armed
    // by M5.Power.lightSleep below, which also waits for it to be released first. A pin that is already
    // low would wake the chip at once, so it is left out.
    static const int kWakePins[] = {Pins::KEY1, Pins::KEY2, Pins::PM1_IRQ};
    bool armed[3] = {false, false, false};
    bool anyArmed = false;
    for (int i = 0; i < 3; ++i) {
        pinMode(kWakePins[i], INPUT_PULLUP);
        if (digitalRead(kWakePins[i]) == LOW) {
            ESP_LOGW(TAG, "GPIO %d is low going to sleep; not arming it", kWakePins[i]);
            continue;
        }
        armed[i] = gpio_wakeup_enable((gpio_num_t)kWakePins[i], GPIO_INTR_LOW_LEVEL) == ESP_OK;
        anyArmed = anyArmed || armed[i];
    }
    if (anyArmed) esp_sleep_enable_gpio_wakeup();

    const int64_t before = esp_timer_get_time();
    M5.Power.lightSleep((uint64_t)sleepMs * 1000ULL, /*touch_wakeup=*/true);
    s_sleptMs += (esp_timer_get_time() - before) / 1000;

    for (int i = 0; i < 3; ++i) {
        if (armed[i]) gpio_wakeup_disable((gpio_num_t)kWakePins[i]);
    }

    WakeReason reason = WakeReason::Other;
    switch (esp_sleep_get_wakeup_cause()) {
    case ESP_SLEEP_WAKEUP_TIMER:
        reason = WakeReason::Timer;
        break;
    case ESP_SLEEP_WAKEUP_EXT0:
        reason = WakeReason::Touch;
        break;
    case ESP_SLEEP_WAKEUP_GPIO:
        // Level wakeups: whichever line is still low is what woke us. A key held down wins over the
        // power button's IRQ line.
        if (digitalRead(Pins::KEY1) == LOW || digitalRead(Pins::KEY2) == LOW) reason = WakeReason::Key;
        else if (digitalRead(Pins::PM1_IRQ) == LOW) reason = WakeReason::PowerButton;
        break;
    default:
        break;
    }
    s_wakes[(int)reason]++;

    if (reason == WakeReason::PowerButton) {
        // The press that woke us is only a wake-up, not a request to power off: clear it, and ignore the
        // button briefly in case M5Unified also latched a click.
        if (_pm1Ready) {
            uint8_t btn = 0;
            _pm1.irqGetBtnStatus(&btn, M5PM1_CLEAN_ALL);
            bool flag = false;
            _pm1.btnGetFlag(&flag);
        }
        _ignorePowerButtonUntilMs = millis() + 1500;
    }
    // A person woke it: bring the light back. A timer wake is a background sync and stays dark.
    if (reason != WakeReason::Timer) setFrontlight(_brightnessBeforeSleep);
    return reason;
}

bool BSP::init(uint8_t initialBrightness) {
    ESP_LOGI(TAG, "Initializing M5Unified and Peripherals (Initial Brightness: %u%%)...", initialBrightness);
    _currentBrightness = initialBrightness;

    // 1. Initialize M5Unified first (sets up M5.In_I2C and board autodetect)
    auto cfg = M5.config();
    cfg.clear_display = false;
    cfg.internal_mic  = false;
    cfg.internal_spk  = false;
    cfg.internal_imu  = false;
    M5.begin(cfg);

    // Immediately enforce desired brightness to eliminate the 100% startup flash
    M5.Display.setBrightness(initialBrightness * 255 / 100);
    M5.Display.setRotation(0);
    M5.Display.setAutoDisplay(false);

    ESP_LOGI(TAG, "M5Unified initialized (Board=%d, Display %dx%d)",
             static_cast<int>(M5.getBoard()), M5.Display.width(), M5.Display.height());

    // 2. Initialize PMIC M5PM1 on M5.In_I2C. Fast Mode (400 kHz) first: begin() verifies the device
    // id at that speed, so a failure is self-detecting and we fall back to the safe 100 kHz.
    for (int attempt = 0; attempt < 2 && !_pm1Ready; ++attempt) {
        const uint32_t speed = (attempt == 0) ? I2C_FREQ_FAST : I2C_FREQ_SAFE;
        for (int retry = 0; retry < 3 && !_pm1Ready; ++retry) {
            if (_pm1.begin(&M5.In_I2C, I2CAddr::PMIC_PM1, speed) == M5PM1_OK) {
                _pm1Ready = true;
                ESP_LOGI(TAG, "M5PM1 PMIC initialized at %u Hz", (unsigned)speed);
                break;
            }
            delay(100);
        }
    }

    if (_pm1Ready) {
        _pm1.setI2cSleepTime(0);
        _pm1.irqClearGpioAll();
        _pm1.irqClearSysAll();
        _pm1.gpioSetWakeEnable(M5PM1_GPIO_NUM_0, false);
        _pm1.gpioSetWakeEnable(M5PM1_GPIO_NUM_4, false);

        // Crucial for battery power hold & software button capture
        _pm1.ldoSetPowerHold(true);
        _pm1.setSingleResetDisable(true);

        // Clear initial button flag/IRQ so boot press is not seen as immediate shutdown
        uint8_t initIrq = 0;
        _pm1.irqGetBtnStatus(&initIrq, M5PM1_CLEAN_ALL);
        bool initFlag = false;
        _pm1.btnGetFlag(&initFlag);
    } else {
        ESP_LOGE(TAG, "M5PM1 PMIC init failed!");
    }

    // 3. Initialize IO Expander M5IOE1 on M5.In_I2C (same 400 kHz probe, then 100 kHz fallback).
    // Done before any pin configuration so a failed probe cannot leave the rails in a wrong state.
    for (int attempt = 0; attempt < 2 && !_ioe1Ready; ++attempt) {
        const uint32_t speed = (attempt == 0) ? I2C_FREQ_FAST : I2C_FREQ_SAFE;
        for (int retry = 0; retry < 3 && !_ioe1Ready; ++retry) {
            if (_ioe1.begin(&M5.In_I2C, I2CAddr::IO_EXPANDER, speed, -1, M5IOE1_INT_MODE_DISABLED) == M5IOE1_OK) {
                _ioe1Ready = true;
                ESP_LOGI(TAG, "M5IOE1 IO Expander initialized at %u Hz", (unsigned)speed);
                break;
            }
            delay(100);
        }
    }

    if (_ioe1Ready) {
        // EPD Power & Reset
        _ioe1.pinMode(IOEPins::EPD_3V3_EN, OUTPUT);
        _ioe1.setDriveMode(IOEPins::EPD_3V3_EN, M5IOE1_DRIVE_PUSHPULL);
        _ioe1.digitalWrite(IOEPins::EPD_3V3_EN, HIGH); // Enable EPD 3.3V power

        _ioe1.pinMode(IOEPins::EPD_RST, OUTPUT);
        _ioe1.setDriveMode(IOEPins::EPD_RST, M5IOE1_DRIVE_PUSHPULL);

        // Touch Power & Reset
        _ioe1.pinMode(IOEPins::TOUCH_VDD_EN, OUTPUT);
        _ioe1.setDriveMode(IOEPins::TOUCH_VDD_EN, M5IOE1_DRIVE_PUSHPULL);
        _ioe1.digitalWrite(IOEPins::TOUCH_VDD_EN, HIGH); // Enable Touch VDD

        _ioe1.pinMode(IOEPins::TOUCH_RST, OUTPUT);
        _ioe1.setDriveMode(IOEPins::TOUCH_RST, M5IOE1_DRIVE_PUSHPULL);

        // Hardware reset pulses
        _ioe1.digitalWrite(IOEPins::EPD_RST, LOW);
        delay(10);
        _ioe1.digitalWrite(IOEPins::EPD_RST, HIGH);
        delay(20);

        _ioe1.digitalWrite(IOEPins::TOUCH_RST, LOW);
        delay(10);
        _ioe1.digitalWrite(IOEPins::TOUCH_RST, HIGH);
        delay(20);

        // RGB LED pins
        _ioe1.pinMode(IOEPins::RGB_GREEN, OUTPUT);
        _ioe1.pinMode(IOEPins::RGB_BLUE, OUTPUT);
        _ioe1.setDriveMode(IOEPins::RGB_GREEN, M5IOE1_DRIVE_PUSHPULL);
        _ioe1.setDriveMode(IOEPins::RGB_BLUE, M5IOE1_DRIVE_PUSHPULL);
        _ioe1.setPwmFrequency(5000);

        // Keep the IP2315 charger disconnected from I2C bus to prevent bus lockups
        _ioe1.pinMode(IOEPins::CHG_I2C_EN, OUTPUT);
        _ioe1.setDriveMode(IOEPins::CHG_I2C_EN, M5IOE1_DRIVE_PUSHPULL);
        _ioe1.digitalWrite(IOEPins::CHG_I2C_EN, LOW);
    } else {
        ESP_LOGE(TAG, "M5IOE1 IO Expander init failed!");
    }

    // User buttons setup
    pinMode(Pins::KEY1, INPUT_PULLUP);
    pinMode(Pins::KEY2, INPUT_PULLUP);

    // Hold the buzzer silent.
    pinMode(Pins::BUZZER_PWM, OUTPUT);
    digitalWrite(Pins::BUZZER_PWM, LOW);

    // Force the PM1 status LED off: after power-on it defaults to RED.
    setLed(false, 0, 0);

    return true;
}

void BSP::setFrontlight(uint8_t brightnessPercent) {
    if (brightnessPercent > 100) brightnessPercent = 100;
    _currentBrightness = brightnessPercent;
    M5.Display.setBrightness(brightnessPercent * 255 / 100);
}

void BSP::setLed(bool red, uint8_t greenPercent, uint8_t bluePercent) {
    if (_pm1Ready) {
        _pm1.setLedEnLevel(red);
    }
    if (_ioe1Ready) {
        _ioe1.setPwmDuty(M5IOE1_PWM_CH2, greenPercent, false, greenPercent > 0);
        _ioe1.setPwmDuty(M5IOE1_PWM_CH1, bluePercent, false, bluePercent > 0);
    }
}

void BSP::setLedSyncOk(uint16_t durationMs) {
    setLed(false, 100, 0);
    _ledOffUntil = millis() + durationMs;
}

void BSP::setLedSyncFailed(uint16_t durationMs) {
    setLed(true, 0, 0);
    _ledOffUntil = millis() + durationMs;
}

BatteryState BSP::getBatteryState() {
    static uint32_t s_lastCheck = 0;
    static uint32_t s_lastVinCheck = 0;
    static BatteryState s_cachedState = {4000, 100, false};
    uint32_t now = millis();

    // Fast VIN check every 300ms for instantaneous charging plug/unplug feedback
    if (now - s_lastVinCheck >= 300 || s_lastVinCheck == 0) {
        s_lastVinCheck = now;
        uint16_t vinMv = 0;
        if (_pm1Ready && _pm1.readVin(&vinMv) == M5PM1_OK) {
            s_cachedState.isCharging = (vinMv >= 4400);
        } else {
            s_cachedState.isCharging = false;
        }
    }

    if (now - s_lastCheck >= 4000 || s_lastCheck == 0) {
        s_lastCheck = now;
        s_cachedState.voltageMv = M5.Power.getBatteryVoltage();
        if (s_cachedState.voltageMv < 3200) {
            s_cachedState.percentage = 0;
        } else if (s_cachedState.voltageMv >= 4150) {
            s_cachedState.percentage = 100;
        } else {
            s_cachedState.percentage = (s_cachedState.voltageMv - 3200) * 100 / (4150 - 3200);
        }
    }

    return s_cachedState;
}

bool BSP::checkPowerButton() {
    if (!_pm1Ready) return false;
    if (millis() < 2500 || millis() < _ignorePowerButtonUntilMs) {
        // Ignore bootup / power-on transient and clear state
        M5.BtnPWR.wasClicked();
        return false;
    }

    if (M5.BtnPWR.wasClicked()) {
        return true;
    }

    static uint32_t s_lastPoll = 0;
    if (millis() - s_lastPoll >= 100) {
        s_lastPoll = millis();
        bool flag = false;
        if (_pm1.btnGetFlag(&flag) == M5PM1_OK && flag) {
            return true;
        }
    }

    return false;
}

void BSP::powerOff(const char* label) {
    ESP_LOGI(TAG, "Entering Power Off: %s", label ? label : "Power Off");
    drawShutdownScreen(nullptr, nullptr, label ? label : "Power Off");
    shutdownHardware();
}

// Battery protection path: unlike the user-initiated power off this one is not optional, so it says
// why the board is going away. The RTC/PM1 always-on domain keeps drawing ~20 uA regardless.
void BSP::lowBatteryShutdown() {
    BatteryState bs = getBatteryState();
    ESP_LOGW(TAG, "Battery critically low (%d mV, %d%%): shutting down", bs.voltageMv, bs.percentage);
    char subtitle[48];
    snprintf(subtitle, sizeof(subtitle), "%.2f V - charge before power on", (double)bs.voltageMv / 1000.0);
    drawShutdownScreen("Battery Low", subtitle, "Power Off");
    shutdownHardware();
}

void BSP::drawShutdownScreen(const char* title, const char* subtitle, const char* bottomLabel) {
    M5.Display.setRotation(0);
    M5.Display.setEpdMode(m5gfx::epd_mode_t::epd_quality);
    M5.Display.fillScreen(TFT_WHITE);
    M5.Display.setTextColor(TFT_BLACK, TFT_WHITE);
    M5.Display.setTextDatum(textdatum_t::middle_center);

    if (!title) {
        M5.Display.setTextSize(6);
        M5.Display.drawString("Shopping List", 240, 400);
    } else {
        M5.Display.setTextSize(4);
        M5.Display.drawString("Shopping List", 240, 320);
        M5.Display.setTextSize(5);
        M5.Display.drawString(title, 240, 440);
        if (subtitle) {
            M5.Display.setTextSize(2);
            M5.Display.drawString(subtitle, 240, 520);
        }
    }

    M5.Display.setTextDatum(textdatum_t::bottom_right);
    M5.Display.setTextSize(2);
    M5.Display.drawString(bottomLabel ? bottomLabel : "Power Off", 460, 780);

    M5.Display.display();
    M5.Display.waitDisplay();
}

void BSP::shutdownHardware() {
    setFrontlight(0);
    setLed(false, 0, 0);

    if (_ioe1Ready) {
        _ioe1.digitalWrite(IOEPins::EPD_3V3_EN, LOW);
        _ioe1.digitalWrite(IOEPins::TOUCH_VDD_EN, LOW);
    }

    if (_pm1Ready) {
        _pm1.ldoSetPowerHold(false);
        _pm1.shutdown();
    }

    M5.Power.powerOff();
    vTaskDelay(pdMS_TO_TICKS(150));

    // Fallback: esp deep sleep. Never sleep without a wake source: when USB/VBUS is connected the
    // PMIC may refuse to cut the power, and an un-wakeable sleep would leave the device apparently
    // bricked (screen off, no reaction) until a hardware reset. Wake on the PM1 IRQ line (the power
    // button, active low on GPIO 1) plus a 60s timer as a safety net.
    const gpio_num_t pm1Irq = (gpio_num_t)Pins::PM1_IRQ;
    rtc_gpio_deinit(pm1Irq);
    rtc_gpio_init(pm1Irq);
    rtc_gpio_set_direction(pm1Irq, RTC_GPIO_MODE_INPUT_ONLY);
    rtc_gpio_pulldown_dis(pm1Irq);
    rtc_gpio_pullup_en(pm1Irq);
    if (esp_sleep_enable_ext0_wakeup(pm1Irq, 0) != ESP_OK) {
        ESP_LOGW(TAG, "EXT0 wake on PM1 IRQ failed, relying on the timer wake");
    }
    esp_sleep_enable_timer_wakeup(60ULL * 1000000ULL);
    ESP_LOGW(TAG, "Deep sleep fallback: wake on PM1 IRQ (GPIO %d, low) or 60s timer", Pins::PM1_IRQ);
    esp_deep_sleep_start();
}

void BSP::update() {
    if (_ledOffUntil > 0 && millis() >= _ledOffUntil) {
        _ledOffUntil = 0;
        setLed(false, 0, 0);
    }
}

} // namespace ShoppingList
