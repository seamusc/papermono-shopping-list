#include "ui/keyboard.h"

#include <algorithm>
#include <cctype>

#include "hal/epd.h"
#include "ui/widgets.h"

namespace ShoppingList {
namespace {

// ---------------------------------------------------------------- layout
// Key geometry is MonoMesh's on-screen keyboard, proven on this panel; the suggestion strip above
// the input box is new.
constexpr int kOverlayY = 388;
constexpr int kOverlayH = UI::kScreenH - kOverlayY;
constexpr int kSuggestY = kOverlayY;
constexpr int kSuggestH = 41;
constexpr int kBoxX = 6, kBoxY = 434, kBoxW = 468, kBoxH = 46;
constexpr size_t kMaxChars = 60;

constexpr int kSuggestCount = 3;
constexpr int kSuggestMargin = 8;
constexpr int kSuggestGap = 6;

// Touch targets extend this far past each drawn key, closing the gaps between keys.
constexpr int kKeySlop = 2;

enum class KeyKind : uint8_t { Char, Backspace, Shift, Space, Send };

struct Key {
    int16_t x, y, w, h;
    KeyKind kind;
    char normal;  // Char keys: what's typed without shift
    char shifted; // Char keys: what's typed with shift/caps
};

std::vector<Key> buildKeys() {
    std::vector<Key> keys;
    auto row = [&](int y, int x0, int w, const char* normal, const char* shifted) {
        for (int i = 0; normal[i]; ++i) {
            keys.push_back({(int16_t)(x0 + i * (w + 5)), (int16_t)y, (int16_t)w, 54, KeyKind::Char,
                            normal[i], shifted[i]});
        }
    };
    row(488, 7, 42, "1234567890", ",.;:?!\"/-'");
    row(548, 7, 42, "qwertyuiop", "QWERTYUIOP");
    row(608, 12, 46, "asdfghjkl", "ASDFGHJKL");
    row(668, 12, 44, "zxcvbnm", "ZXCVBNM");

    const int backspaceX = 12 + 7 * (44 + 5);
    keys.push_back({(int16_t)backspaceX, 668, (int16_t)(UI::kScreenW - backspaceX - 12), 54, KeyKind::Backspace, 0, 0});
    keys.push_back({10, 728, 100, 58, KeyKind::Shift, 0, 0});
    keys.push_back({120, 728, 236, 58, KeyKind::Space, 0, 0});
    keys.push_back({366, 728, 104, 58, KeyKind::Send, 0, 0});
    return keys;
}

const std::vector<Key>& keys() {
    static const std::vector<Key> k = buildKeys();
    return k;
}

void suggestionRect(int index, int& x, int& y, int& w, int& h) {
    const int usable = UI::kScreenW - 2 * kSuggestMargin - (kSuggestCount - 1) * kSuggestGap;
    const int baseW = usable / kSuggestCount;
    x = kSuggestMargin + index * (baseW + kSuggestGap);
    w = (index == kSuggestCount - 1) ? (UI::kScreenW - kSuggestMargin - x) : baseW;
    y = kSuggestY + 3;
    h = kSuggestH - 6;
}

std::string lowercase(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::tolower(c); });
    return s;
}

void drawShiftIcon(M5GFX& gfx, const Key& k, bool once, bool lock) {
    const int cx = k.x + k.w / 2;
    const int cy = k.y + k.h / 2 + 2;
    const uint16_t fg = lock ? TFT_WHITE : TFT_BLACK;
    const uint16_t bg = lock ? TFT_BLACK : TFT_WHITE;
    gfx.fillTriangle(cx - 11, cy - 2, cx + 11, cy - 2, cx, cy - 15, fg);
    if (!once && !lock) gfx.fillTriangle(cx - 6, cy - 3, cx + 6, cy - 3, cx, cy - 9, bg); // outline only
    gfx.fillRect(cx - 9, cy + 1, 18, 4, fg);
    if (lock) gfx.fillRect(cx - 9, cy + 8, 18, 3, fg);
}

} // namespace

void KeyboardWidget::open(const std::vector<CatalogEntry>* catalog,
                          std::function<void(const KeyboardResult&)> onSend) {
    _open = true;
    _buffer.clear();
    // Auto-capitalize the first letter, Android-sentence-style: shift starts primed and the
    // one-shot gets consumed by typeChar() like any other shift press.
    _shift = Shift::Once;
    _onSend = std::move(onSend);
    _catalog = catalog;
    _suggestions.clear();
    pushOverlay();
}

void KeyboardWidget::updateSuggestions() {
    _suggestions.clear();
    if (_buffer.empty() || _catalog == nullptr) return;

    // Prefix matches first, then substring matches, each in catalog order (alphabetical, as the
    // server sends it).
    const std::string needle = lowercase(_buffer);
    std::vector<const CatalogEntry*> substringMatches;
    for (const auto& entry : *_catalog) {
        const std::string name = lowercase(entry.name.c_str());
        const size_t pos = name.find(needle);
        if (pos == 0 && _suggestions.size() < kSuggestCount) {
            _suggestions.push_back(&entry);
        } else if (pos != std::string::npos && pos != 0) {
            substringMatches.push_back(&entry);
        }
    }
    for (const auto* e : substringMatches) {
        if (_suggestions.size() >= kSuggestCount) break;
        _suggestions.push_back(e);
    }
}

void KeyboardWidget::drawSuggestions(M5GFX& gfx) {
    gfx.fillRect(0, kSuggestY, UI::kScreenW, kSuggestH, TFT_WHITE);
    gfx.setTextDatum(textdatum_t::middle_center);
    gfx.setTextSize(2);
    for (int i = 0; i < (int)_suggestions.size(); ++i) {
        int x, y, w, h;
        suggestionRect(i, x, y, w, h);
        gfx.drawRoundRect(x, y, w, h, 4, TFT_BLACK);
        gfx.fillRoundRect(x + 1, y + 1, w - 2, h - 2, 3, TFT_LIGHTGRAY);
        gfx.setTextColor(TFT_BLACK, TFT_LIGHTGRAY);
        gfx.drawString(_suggestions[i]->name.c_str(), x + w / 2, y + h / 2);
    }
}

void KeyboardWidget::drawInputBox(M5GFX& gfx) {
    gfx.drawRoundRect(kBoxX, kBoxY, kBoxW, kBoxH, 5, TFT_BLACK);
    gfx.fillRoundRect(kBoxX + 2, kBoxY + 2, kBoxW - 4, kBoxH - 4, 4, TFT_LIGHTGRAY);
    gfx.setTextColor(TFT_BLACK, TFT_LIGHTGRAY);
    gfx.setTextDatum(textdatum_t::middle_left);
    gfx.setTextSize(2);

    // Show as much of the end of the buffer as fits, followed by a "_" cursor.
    constexpr int kMaxTextPx = kBoxW - 20;
    std::string shown = "_";
    for (size_t start = _buffer.size(); start-- > 0;) {
        std::string candidate = _buffer.substr(start) + "_";
        if (gfx.textWidth(candidate.c_str()) > kMaxTextPx) break;
        shown = std::move(candidate);
    }
    gfx.drawString(shown.c_str(), kBoxX + 10, kBoxY + kBoxH / 2);
}

void KeyboardWidget::draw(M5GFX& gfx) {
    gfx.fillRect(0, kOverlayY, UI::kScreenW, kOverlayH, TFT_WHITE);
    drawSuggestions(gfx);
    drawInputBox(gfx);

    const bool shifted = _shift != Shift::Off;
    gfx.setTextDatum(textdatum_t::middle_center);
    gfx.setTextSize(2);
    for (const Key& k : keys()) {
        const bool filled = k.kind == KeyKind::Shift && _shift == Shift::Lock;
        const uint16_t bg = filled ? TFT_BLACK
                            : (k.kind == KeyKind::Backspace || k.kind == KeyKind::Send) ? TFT_LIGHTGRAY
                                                                                          : TFT_WHITE;
        gfx.drawRoundRect(k.x, k.y, k.w, k.h, 5, TFT_BLACK);
        gfx.fillRoundRect(k.x + 1, k.y + 1, k.w - 2, k.h - 2, 4, bg);
        gfx.setTextColor(TFT_BLACK, bg);

        const int cx = k.x + k.w / 2;
        const int cy = k.y + k.h / 2;
        switch (k.kind) {
            case KeyKind::Char: {
                const char label[2] = {shifted ? k.shifted : k.normal, 0};
                gfx.drawString(label, cx, cy);
                break;
            }
            case KeyKind::Backspace: gfx.drawString("<-", cx, cy); break;
            case KeyKind::Space: gfx.drawString("SPACE", cx, cy); break;
            case KeyKind::Send: gfx.drawString("SEND", cx, cy); break;
            case KeyKind::Shift: drawShiftIcon(gfx, k, _shift == Shift::Once, _shift == Shift::Lock); break;
        }
    }
}

void KeyboardWidget::pushInputArea() {
    draw(M5.Display);
    Epd::getInstance().partialUpdate(0, kSuggestY, UI::kScreenW, (kBoxY + kBoxH) - kSuggestY);
}

void KeyboardWidget::pushOverlay() {
    draw(M5.Display);
    Epd::getInstance().partialUpdate(0, kOverlayY, UI::kScreenW, kOverlayH);
}

void KeyboardWidget::typeChar(char c) {
    if (_buffer.size() >= kMaxChars) return;
    _buffer += c;
    updateSuggestions();
    if (_shift == Shift::Once) {
        // Consuming a one-shot shift changes every keycap, not just the input area.
        _shift = Shift::Off;
        pushOverlay();
    } else {
        pushInputArea();
    }
}

void KeyboardWidget::handleTouch(const TouchEvent& ev) {
    if (!_open) return;

    const bool tap = ev.type == TouchEventType::Click;
    if (ev.type == TouchEventType::SwipeRight ||
        ((tap || ev.type == TouchEventType::Up) && ev.y < kOverlayY)) {
        close();
        return;
    }
    if (!tap) return;

    if (ev.y >= kSuggestY && ev.y < kSuggestY + kSuggestH) {
        for (int i = 0; i < (int)_suggestions.size(); ++i) {
            int x, y, w, h;
            suggestionRect(i, x, y, w, h);
            if (ev.x >= x && ev.x < x + w) {
                _buffer = _suggestions[i]->name.c_str();
                updateSuggestions();
                pushOverlay();
                return;
            }
        }
        return;
    }

    for (const Key& k : keys()) {
        if (!UI::inRect(ev, k.x - kKeySlop, k.y - kKeySlop, k.w + 2 * kKeySlop, k.h + 2 * kKeySlop)) continue;
        switch (k.kind) {
            case KeyKind::Char:
                typeChar(_shift != Shift::Off ? k.shifted : k.normal);
                break;
            case KeyKind::Space:
                // Unlike a letter, a space doesn't use up a one-shot shift.
                if (_buffer.size() < kMaxChars) {
                    _buffer += ' ';
                    updateSuggestions();
                    pushInputArea();
                }
                break;
            case KeyKind::Backspace:
                if (!_buffer.empty()) {
                    _buffer.pop_back();
                    updateSuggestions();
                    if (_buffer.empty() && _shift == Shift::Off) {
                        // Back to a blank item name - re-prime auto-capitalize, same as on open().
                        _shift = Shift::Once;
                        pushOverlay();
                    } else {
                        pushInputArea();
                    }
                }
                break;
            case KeyKind::Shift:
                _shift = _shift == Shift::Off ? Shift::Once : _shift == Shift::Once ? Shift::Lock : Shift::Off;
                pushOverlay();
                break;
            case KeyKind::Send: {
                KeyboardResult result;
                result.text = _buffer;
                for (const auto* s : _suggestions) {
                    if (_buffer == s->name.c_str()) {
                        result.categoryId = s->categoryId;
                        break;
                    }
                }
                close();
                if (_onSend) _onSend(result);
                break;
            }
        }
        return;
    }
}

} // namespace ShoppingList
