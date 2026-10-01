#pragma once

// Board support for the M5Stack PaperMono: power-management IC (M5PM1), IO expander (M5IOE1), rails
// and resets for the panel and touch controller, battery, power button, status LED and frontlight.
//
// Adapted from MonoMesh's bsp_papermono (see docs/design-notes.md): the bring-up sequence and the
// power-off path are kept as they were; LoRa, RTC and MonoMesh's low-power clock modes are not.

#include <Arduino.h>
#include <Wire.h>
#include <M5PM1.h>
#include <M5IOE1.h>
#include <M5Unified.h>

namespace ShoppingList {

// Pin Definitions
namespace Pins {
    // System I2C
    constexpr int I2C_SDA = 47;
    constexpr int I2C_SCL = 48;

    // Keys
    constexpr int KEY1 = 2; // Button A
    constexpr int KEY2 = 3; // Button B
    constexpr int PM1_IRQ = 1; // PMIC M5PM1 IRQ line (power button wake, RTC-capable GPIO)

    // Display (SSD1677 on SPI2)
    constexpr int EPD_MOSI = 14;
    constexpr int EPD_SCLK = 15;
    constexpr int EPD_CS   = 16;
    constexpr int EPD_DC   = 17;
    constexpr int EPD_BUSY = 18;

    // Touch (FT6336G)
    constexpr int TOUCH_INT = 4;

    // Buzzer (unused; held silent)
    constexpr int BUZZER_PWM = 42;
}

// I2C Addresses
namespace I2CAddr {
    constexpr uint8_t PMIC_PM1    = 0x6E;
    constexpr uint8_t IO_EXPANDER = 0x4F;
    constexpr uint8_t TOUCH_FT    = 0x38;
}

// M5IOE1 pin assignments. The LoRa radio's antenna-switch and reset lines are deliberately left
// alone: the radio is populated on the C153 but this firmware never uses it.
namespace IOEPins {
    constexpr uint8_t EPD_3V3_EN  = M5IOE1_PIN_3;
    constexpr uint8_t EPD_RST     = M5IOE1_PIN_5;
    constexpr uint8_t TOUCH_RST   = M5IOE1_PIN_6;
    constexpr uint8_t RGB_GREEN   = M5IOE1_PIN_8;
    constexpr uint8_t RGB_BLUE    = M5IOE1_PIN_9;
    constexpr uint8_t CHG_I2C_EN  = M5IOE1_PIN_11;
    constexpr uint8_t TOUCH_VDD_EN= M5IOE1_PIN_13;
}

// What ended a light sleep.
enum class WakeReason { Timer, Touch, Key, PowerButton, Other };

struct BatteryState {
    int voltageMv;
    int percentage;
    bool isCharging;
};

class BSP {
public:
    static BSP& getInstance() {
        static BSP instance;
        return instance;
    }

    // Frontlight defaults to off: e-paper is readable in normal light, and the light is the biggest
    // avoidable battery drain. Adjustable from the settings screen.
    bool init(uint8_t initialBrightness = 0);
    void update();
    bool checkPowerButton();
    void powerOff(const char* label = "Power Off");
    // Battery protection: draws a warning screen and powers the board down.
    void lowBatteryShutdown();
    // Cut-off on the resting pack voltage, from MonoMesh's measured discharge curve. Protection works
    // on millivolts because the percentage estimate reads optimistic near empty.
    static constexpr int LOW_BATTERY_CUTOFF_MV = 3450;

    void setFrontlight(uint8_t brightnessPercent);
    uint8_t getFrontlight() const { return _currentBrightness; }
    // Brief green pulse: a WiFi sync completed successfully.
    void setLedSyncOk(uint16_t durationMs = 200);
    // Brief red pulse: a sync attempt failed (offline, server unreachable, ...).
    void setLedSyncFailed(uint16_t durationMs = 200);

    BatteryState getBatteryState();

    // Light-sleeps for at most `sleepMs` and returns what woke the device. Timer, a touch (the touch
    // controller's interrupt), a side key and the power button all wake it. Everything is as it was on
    // return, except that the frontlight and LED are off until a wake for a person restores the light.
    // Finish any panel refresh first; this waits for one that's running but starts none.
    WakeReason lightSleep(uint32_t sleepMs);

    // One line of power history since the last successful sync, sent as the X-Power request header so
    // the server log shows what the device has really been doing:
    //   "timer=2;touch=1;key=0;pb=0;other=0;awake_ms=31400;slept_ms=600000"
    // Call resetPowerStats() once a sync succeeds.
    static String powerStats();
    static void resetPowerStats();

private:
    BSP() = default;
    ~BSP() = default;

    void setLed(bool red, uint8_t greenPercent, uint8_t bluePercent);
    void drawShutdownScreen(const char* title, const char* subtitle, const char* bottomLabel);
    void shutdownHardware();

    M5PM1 _pm1;
    M5IOE1 _ioe1;
    bool _pm1Ready = false;
    bool _ioe1Ready = false;
    uint8_t _currentBrightness = 0;
    uint8_t _brightnessBeforeSleep = 0;
    uint32_t _ignorePowerButtonUntilMs = 0;
    uint32_t _ledOffUntil = 0;
};

} // namespace ShoppingList
