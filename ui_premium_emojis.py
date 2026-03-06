import os
from typing import Optional

# ============================================================
# UI PREMIUM EMOJI CONFIG (единый блок)
# Меняйте emoji-id здесь или через .env.
# Пустое значение = fallback (обычный эмодзи).
# ============================================================

PREMIUM_TEXT_EMOJI = {
    # Game invites / headers
    "duel_invite": os.getenv("PE_DUEL_INVITE", "5280816565657300091"),
    "duel_dice_count": os.getenv("PE_DUEL_DICE_COUNT", "5280816565657300091"),
    "blackjack_invite": os.getenv("PE_BLACKJACK_INVITE", "6028206863038811654"),
    "knb_invite": os.getenv("PE_KNB_INVITE", "5269640498112378277"),
    "multi_invite": os.getenv("PE_MULTI_INVITE", "5280816565657300091"),
    "multi_participants": os.getenv("PE_MULTI_PARTICIPANTS", "5906852613629941703"),
    "multi_waiting": os.getenv("PE_MULTI_WAITING", "5337258577729961075"),

    # Common UI emoji
    "dice": os.getenv("PE_TXT_DICE", "5280816565657300091"),
    "clipboard": os.getenv("PE_TXT_CLIPBOARD", ""),
    "info": os.getenv("PE_TXT_INFO", ""),
    "chart": os.getenv("PE_TXT_CHART", "5424714857884705985"),
    "target": os.getenv("PE_TXT_TARGET", "5274266216544871353"),
    "finish_flag": os.getenv("PE_TXT_FINISH_FLAG", ""),
    "trophy": os.getenv("PE_TXT_TROPHY", "5273899469287465771"),
    "money_bag": os.getenv("PE_TXT_MONEY_BAG", "5332600543963522398"),
    "link": os.getenv("PE_TXT_LINK", "5372845104786600978"),
    "handshake": os.getenv("PE_TXT_HANDSHAKE", "6034834452843074121"),
    "cross_mark": os.getenv("PE_TXT_CROSS_MARK", "5271934564699226262"),
    "credit_card": os.getenv("PE_TXT_CREDIT_CARD", "5472250091332993630"),
    "question_mark": os.getenv("PE_TXT_QUESTION_MARK", "5238224607638468926"),
    "gamepad": os.getenv("PE_TXT_GAMEPAD", "5319247469165433798"),
    "blackjack": os.getenv("PE_TXT_BLACKJACK", "6028206863038811654"),
    "rock": os.getenv("PE_TXT_ROCK", "5269640498112378277"),
    "paper": os.getenv("PE_TXT_PAPER", "5267118828323616839"),
    "scissors": os.getenv("PE_TXT_SCISSORS", "5269616738353300957"),
    "cash": os.getenv("PE_TXT_CASH", "5388591696139269118"),
    "chart_up": os.getenv("PE_TXT_CHART_UP", "5436356233596516554"),
    "lock": os.getenv("PE_TXT_LOCK", "5283097506824071299"),
    "memo": os.getenv("PE_TXT_MEMO", "6028220478085140634"),
    "bulb": os.getenv("PE_TXT_BULB", "5906891238270834298"),
    "telephone": os.getenv("PE_TXT_TELEPHONE", "5435955998479102657"),
    "speech_balloon": os.getenv("PE_TXT_SPEECH_BALLOON", "5303138782004924588"),
    "scales": os.getenv("PE_TXT_SCALES", "5400250414929041085"),
    "check_mark": os.getenv("PE_TXT_CHECK_MARK", "5273806972871787310"),
    "bust_in_silhouette": os.getenv("PE_TXT_BUST_IN_SILHOUETTE", "5199445141764475415"),
    "warning": os.getenv("PE_TXT_WARNING", "5269744182917866822"),
    "wave": os.getenv("PE_TXT_WAVE", "5388587564380728115"),
    "green_circle": os.getenv("PE_TXT_GREEN_CIRCLE", ""),
    "down_arrow": os.getenv("PE_TXT_DOWN_ARROW", ""),

}

PREMIUM_BUTTON_EMOJI = {
    "accept": os.getenv("PE_BTN_ACCEPT", "5273806972871787310"),
    "decline": os.getenv("PE_BTN_DECLINE", "5271934564699226262"),
    "multi_join": os.getenv("PE_BTN_MULTI_JOIN", "5273806972871787310"),
    "menu_profile": os.getenv("PE_BTN_MENU_PROFILE", ""),
    "menu_stats": os.getenv("PE_BTN_MENU_STATS", ""),
    "menu_top": os.getenv("PE_BTN_MENU_TOP", ""),
    "menu_info": os.getenv("PE_BTN_MENU_INFO", ""),
    "menu_help": os.getenv("PE_BTN_MENU_HELP", ""),
}

# Автозамена символов в UI-текстах
CHAR_TO_KEY = {
    "🎲": "dice",
    "📋": "clipboard",
    "ℹ️": "info",
    "📊": "chart",
    "🎯": "target",
    "🏁": "finish_flag",
    "🏆": "trophy",
    "💰": "money_bag",
    "🔗": "link",
    "🤝": "handshake",
    "❌": "cross_mark",
    "💳": "credit_card",
    "❓": "question_mark",
    "🎮": "gamepad",
    "🃏": "blackjack",
    "🗿": "rock",
    "📄": "paper",
    "✂️": "scissors",
    "💵": "cash",
    "📈": "chart_up",
    "🔒": "lock",
    "📝": "memo",
    "💡": "bulb",
    "📞": "telephone",
    "💬": "speech_balloon",
    "⚖️": "scales",
    "✅": "check_mark",
    "👤": "bust_in_silhouette",
    "👥": "bust_in_silhouette",
    "⚠️": "warning",
    "👋": "wave",
    "🟢": "green_circle",
    "⬇️": "down_arrow",
}


def premium_emoji(key: str, fallback: str) -> str:
    emoji_id = (PREMIUM_TEXT_EMOJI.get(key) or "").strip()
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def premiumize_text(text: str) -> str:
    result = text
    for char, key in sorted(CHAR_TO_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        emoji_id = (PREMIUM_TEXT_EMOJI.get(key) or "").strip()
        if not emoji_id:
            continue
        result = result.replace(char, f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>')
    return result


def build_button_api_kwargs(style: Optional[str] = None, emoji_key: Optional[str] = None) -> dict:
    kwargs: dict = {}
    if style:
        kwargs["style"] = style
    if emoji_key:
        emoji_id = (PREMIUM_BUTTON_EMOJI.get(emoji_key) or "").strip()
        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id
    return kwargs
