"""Every Russian user-facing string the bot can send, in one place. The only other files
allowed to hold Cyrillic literals are the prompt modules in app/llm/ — those speak Russian to
the models, not to the user.

This module is a leaf: it imports nothing of the app's own."""

from __future__ import annotations

# ---- buttons ----------------------------------------------------------------------------------

BTN_UNDO = "Отменить"
BTN_FIX = "Поправить"
# Under a mail digest: mark every letter it listed as read, and the way back.
BTN_MAIL_READ = "✓ Отметить прочитанными"
BTN_MAIL_UNREAD = "↩ Вернуть непрочитанными"

UNTITLED = "(без названия)"

# ---- standalone replies -----------------------------------------------------------------------

# Sent the moment a voice note arrives, before the download and `speech.transcribe` — the first
# one of a run loads (and, the very first time, downloads ~1.5 GB of) the Whisper model, which can
# take minutes during which the bot would otherwise be indistinguishable from a dead one.
VOICE_TRANSCRIBING = "Расшифровываю голосовое сообщение…"
UNDONE = "↩️ Отменено."

# ---- errors (documentation/ERRORS.md), keyed by error code ------------------------------------

# Placeholders here are filled by app.conversation.orchestrator, which degrades to
# ERRORS["INTERNAL"] for any template it cannot fill — so a template may only ask for a value
# its caller actually has. test_texts.py pins the placeholder set of every code against that.
ERRORS: dict[str, str] = {
    # The vault is switched off on the admin page: there is nowhere to write.
    "NOTHING_ENABLED": "Obsidian выключен — включите его на странице настроек.",
    # No pipeline was built at startup: the switch cannot help, .env can.
    "VAULT_NOT_SET": "Хранилище не подключено: нужны OBSIDIAN_VAULT и ANTHROPIC_API_KEY в .env.",
    "STT_EMPTY": "Не разобрал голосовое сообщение. Повторите или напишите текстом.",
    "STT_FAILED": "Ошибка распознавания речи.",
    "UNDO_EXPIRED": "Отменить уже нельзя (прошло больше {minutes} минут).",
    "UNDO_FAILED": "Не удалось отменить: {message}.",
    # A button from an older version of the bot, which nothing answers any more.
    "BUTTON_EXPIRED": "Эта кнопка больше не работает.",
    "INTERNAL": "Не удалось обработать сообщение.",
    "LINK_ANSWER_FAILED": "Не получилось ответить по ссылке — попробуйте спросить ещё раз.",
}

# ---- /start, /help (app.telegram.handlers) ----------------------------------------------------

HELP_TEXT = (
    "Я веду ваше хранилище Obsidian по текстовым и голосовым сообщениям: задачи со сроками и "
    "повторами, заметки, продукты, дневник, напоминания.\n"
    "Просто напишите или наговорите, что нужно, — или спросите: «что у меня в пятницу», "
    "«что там про борщ».\n\n"
    "Команды:\n"
    "/start — начать работу\n"
    "/help — это сообщение\n"
    "/undo — отменить последнее действие\n\n"
    "Любую запись можно отменить или поправить кнопками под ответом. Умею и искать в "
    "интернете: «найди рецепт борща и запиши в заметки»."
)

# The command menu Telegram shows behind the "/" button, published by app.main._register_commands
# via set_my_commands. Keys are command names without the slash; the order here is the order the
# menu shows. Every command registered in app.telegram.handlers.register belongs here, and
# HELP_TEXT lists the same set in prose.
COMMANDS: dict[str, str] = {
    "start": "Начать работу",
    "help": "Что умеет бот и какие есть команды",
    "undo": "Отменить последнее действие",
}

# ---- when Claude cannot be used (app/llm/health.py) -------------------------------------------

# A few words for a line that is already about a failure ("Obsidian — не записано: …").
LLM_DOWN_SHORT: dict[str, str] = {
    "credit": "закончились кредиты Claude",
    "key": "ключ Claude не принят",
    "limit": "слишком много запросов к Claude",
    "down": "Claude не отвечает",
}
# The whole sentence, appended to a reply at most once an hour. It says what broke, what that
# costs the user right now, and what to do about it — a warning that names none of the three
# is the silence it replaces.
LLM_DOWN_NOTE: dict[str, str] = {
    "credit": ("⚠️ На счёте Anthropic закончились кредиты — пока сообщения остаются как есть в "
               "«Разное», а на вопросы бот не отвечает. "
               "Пополнить: https://console.anthropic.com/settings/billing"),
    "key": ("⚠️ Claude не принимает ключ — проверьте ANTHROPIC_API_KEY. Пока сообщения остаются "
            "как есть в «Разное», а на вопросы бот не отвечает."),
    "limit": ("⚠️ Слишком много запросов к Claude подряд — через несколько минут всё вернётся "
              "само."),
    "down": ("⚠️ Claude сейчас не отвечает — пока сообщения остаются как есть в «Разное», а на "
             "вопросы бот не отвечает."),
}

# ---- Obsidian vault ---------------------------------------------------------------------------

# The vault's layout: folder and note names the user sees in Obsidian. Renaming one here
# renames it for every note written later, not for notes already in the vault.
VAULT_TASKS_NOTE = "Задачи"
VAULT_ARCHIVE_NOTE = "Задачи — архив"
VAULT_AREAS_DIR = "Области"
VAULT_NOTES_DIR = "Заметки"
# Templater's templates. Not notes: the index skips them and the writer never writes there.
VAULT_TEMPLATES_DIR = "Шаблоны"
VAULT_FILES_DIR = "Вложения"
VAULT_DAILY_DIR = "Дневник"
VAULT_HOME_NOTE = "Главная"
VAULT_NO_TAG_HEADING = "Без тэга"
VAULT_COUNTDOWN_TAG = "отсчёт"
# Frontmatter properties a note may carry its own date in (a meeting, a trip, a birthday).
VAULT_DATE_PROPS = ("date", "дата", "when", "due", "start")
# The grocery page: a registry, not a to-do list (app/vault/groceries.py). Every product has
# one permanent line — ticked means in stock, unticked means buy it — so the page never grows
# except for a genuinely new product, and nothing is ever archived. The home page finds the
# unticked ones by file name, so the lines carry no tag.
# Words in front of a product that are not part of its name, so "купить 2 л молока"
# registers as "молока". Units are only stripped after a number — on their own
# they would eat the product ("г" is also the start of "гречка").
# Letters that sort out of alphabetical order by code point, folded before sorting the
# grocery page: ё sits after я, so a product starting with it would land at the bottom
# of an otherwise alphabetical list.
VAULT_SORT_FOLD = {"ё": "е"}
# Word beginnings that mean a message names a time. A date the model returns counts only when
# the message has a digit or one of these: a model asked for a date tends to supply one.
# Words that ask for a whole set of chores («всё что нужно взять», «список дел для переезда»).
# Without one, a lookup on the task file is the model inventing work: «надо внести траты»
# came back as a five-step checklist.
# Asking for something to be worked out rather than written down («сравни варианты и
# придумай план поездки»): the vault may search for it, as it does for «найди».
# Words that name a page as the thing to make («создай страницу», «новую заметку»): with a make
# word, a new note even where the place picked is an existing one.
VAULT_PAGE_WORDS = ("страниц", "страничк", "заметк")
# A page word after one of these names a place, not a thing to make («в заметку»).
VAULT_PLACE_PREPOSITIONS = ("в", "во", "к", "на")
VAULT_COMPOSE_CUES = ("придума", "спланир", "сравни", "составь план", "составь маршрут",
                      "подбери", "посоветуй")
VAULT_SET_CUES = ("все", "всё", "всех", "весь", "вся", "список", "перечисл",
                  "что взять", "что нужно", "что надо", "полный", "целиком")
# Words that say a time of day without a number of its own ("в час дня", "утром"): with
# one of them, a task's time need not repeat a number from the message.
VAULT_CLOCK_CUES = ("утр", "вечер", "дня", "ночи", "полдень", "полдн", "полноч", "час")
# A request to tick tasks off, as opposed to a report of something done.
VAULT_TICK_CUES = ("отметь", "отметить", "отмечай", "вычеркн", "закрой", "закрыть",
                   "галочк")
VAULT_DATE_CUES = (
    "сегодн", "завтр", "послезавтр", "вчера", "понедельник", "вторник", "сред", "четверг",
    "пятниц", "суббот", "воскресен", "недел", "месяц", "выходн", "через", "январ", "феврал",
    "март", "апрел", "мая", "май", "июн", "июл", "август", "сентябр", "октябр", "ноябр",
    "декабр", "утр", "вечер", "ноч", "числ", "каждый", "каждую", "каждое", "ежедневн",
    "today", "tomorrow",
)
VAULT_GROCERY_UNITS = ("л", "мл", "кг", "г", "гр", "шт", "уп")
# Containers, which are unambiguous enough to strip on their own ("пачка соли").
VAULT_GROCERY_PACKS = ("пачк", "банк", "бутыл", "пара", "пары")
# Both tenses: "buy milk" names a product to get, "bought milk" names the same product as
# already got, and the name has to survive either.
VAULT_BUY_WORDS = ("купить", "купи", "куплю", "купили", "купила", "купил",
                   "закупился", "закупить", "заказать", "закажи", "заказали", "заказала",
                   "заказал", "взять", "возьми", "взяли", "взяла", "взял", "есть")
VAULT_GROCERIES_NOTE = "Продукты"
VAULT_GROCERIES_INTRO = ("Отмеченные — есть дома. Снимите галочку с того, что нужно "
                         "купить — оно появится на Главной. Сроков и повторов здесь не бывает.")
# The answer to "что надо купить?"
VAULT_GROCERIES_LIST = "🛒 Купить: {items}"
VAULT_GROCERIES_EMPTY = "🛒 Всё есть — покупать нечего."
# One line in the morning digest, only when something is actually needed.
VAULT_GROCERIES_DIGEST = "🛒 Продукты ({n}): {items}"
VAULT_INBOX_NOTE = "Разное"
# A question answered from the vault: the header, one line per hit, and the empty answer.
VAULT_SEARCH_HEADER = "🔍 Obsidian — нашёл:"
VAULT_SEARCH_HIT = "• {name}{line}"
VAULT_SEARCH_TASK = "• {line}"
VAULT_SEARCH_EMPTY = "🔍 Obsidian — ничего не нашёл."
# An answer read from the notes (app/vault/answer.py), then the notes it came from.
VAULT_ANSWER = "🔍 {answer}"
VAULT_ANSWER_SOURCES = "📎 {links}"
# Words a question is built from rather than about: they match everything and mean nothing.
SEARCH_STOP_WORDS = frozenset({
    "найди", "найти", "покажи", "напомни", "скажи", "дай", "хочу", "нужно", "надо",
    "страница", "страницу", "страницы", "заметка", "заметку", "заметки", "файл",
    "мою", "моя", "мои", "моего", "меня", "тебя", "себе", "там", "тут", "здесь",
    "что", "чего", "какие", "какой", "какая", "когда", "куда", "если", "было", "есть",
    "про", "для", "это", "этот", "эта", "вообще", "потом", "ещё", "еще",
})
# One line per write, appended to the reply: what the Obsidian side did.
VAULT_REPLY = "✅ Obsidian — {what}"
VAULT_FAILED = "⚠️ Obsidian — не записано: {error}"
# A group chat with its own scope (app/chats.py, app/vault/scope.py).
# Chat answers and memory (app/conversation/memory.py, app/vault/answer.py).
MEMORY_USER = "Пользователь"
MEMORY_BOT = "Ассистент"
POINTED_ANSWER = "последний ответ ассистента"
POINTED_EARLIER = "ответ ассистента раньше"
POINTED_QUOTE = "сообщение, на которое отвечают"
ANSWER_GENERAL = "💬 {answer}"
# A multi-step message (app/vault/planner.py): a part that could not be done, and what a write
# step reports to the steps after it.
PLAN_PART_FAILED = "⚠️ Не получилось: {what}"
PLAN_PART_FAILED_WHY = "⚠️ Не получилось: {what} — {why}"
PLAN_STEP_WROTE = "записано: {what}"
PLAN_STEP_UNCLEAR = "шаг не понят"
PLAN_SEARCHES_SPENT = "поиски для этого сообщения уже потрачены"
# The answerer asking who the user is in the records before answering about them; the next
# short message is taken as the name.
ANSWER_WHO = "❓ {lead}Как вы записаны? Напишите своё имя так, как оно там."
ANSWER_WHO_OPTIONS = "❓ {lead}Кто из них вы: {options}? Напишите, как вы записаны."
WHO_NOT_HERE = "Здесь нет {names}. "
# Words before the name in a reply to that question («я женя», «меня зовут Саша»).
ASKER_LEADS = ("я", "это я", "меня зовут", "зовут", "там я", "я там", "я записан как",
               "я записана как", "записан как", "записана как", "я —", "я -")
ANSWER_WHO_NOT_FOUND = "🔍 Не нашёл в записях {names}."
# Words between the names in one reply («соня или лиза»); commas, «;» and «/» too.
ASKER_SEPARATORS = ("или", "либо", "и", "or")
# A question about the asker themselves: then their names in the records are searched for too.
ASKER_SELF_WORDS = ("я", "мне", "меня", "мной", "мой", "моя", "моё", "мое", "мои", "моих",
                    "моим", "моей", "моего", "моему", "моими", "моё")
ANSWER_WEB = "🌐 {answer}"
ANSWER_SEARCHING = "🔎 Ищу в интернете…"
ANSWER_WEB_ASK = "🌐 Чтобы поискать, уточните: {question}"
ANSWER_WEB_FAILED = "🌐 Поискать в интернете не получилось: {error}"
# A question that asks to write something down is filed, not answered.
VAULT_WRITE_CUES = ("запиш", "сохран", "добав", "созда", "заметк", "внес", "план",
                    "отмет", "в список", "в задач", "напомн", "занеси")
# Words that point back at something said before ("запиши это"): whole words, and stems.
BACK_WORDS = ("это", "этот", "эту", "эти", "то", "тот", "ту", "выше")
BACK_STEMS = ("последн", "написал", "сказал", "предыдущ", "прислал", "скинул")
# Google sources (app/google/): how their text is laid out for the answerer.
GOOGLE_PIECE = "{title} (часть {n})"
GOOGLE_ROWS = "{title} (строки, часть {n})"
GOOGLE_COLUMN = "колонка {n}"
GOOGLE_SUMMARY = "{title} (сводка по колонкам: сколько чисел, среднее, наибольшие и наименьшие)"
GOOGLE_SCORES = "Больше всего баллов — {top}\nМеньше всего баллов — {bottom}"
GOOGLE_CUT = "[… дальше не показано]"
GOOGLE_EXTREMES = "{column}: больше всего — {top}; меньше всего — {bottom}"
GOOGLE_COUNTS = ("Строк: {rows}. По каждой колонке — сколько строк с числом и среднее этих "
                 "чисел: {counts}")
GOOGLE_COUNT = "{column} {n} (ср. {mean})"
# The end of a sheet's row: how many numbers it holds, counted by the bot; the columns that only
# total the others (min, max, mean, sum of the row) are not among them.
GOOGLE_ROW_NUMBERS = "[чисел: {n}]"
# A sheet's column as a section of its own: every number in it, highest first, by row.
GOOGLE_COLUMN_SECTION = "{title} (колонка «{column}»: все числа по убыванию)"
GOOGLE_COLUMN_HEAD = "{column}: чисел {n}, среднее {mean}; по убыванию:"
GOOGLE_COLUMN_SKIPPED = "[… ещё {n} в середине не показаны]"
GOOGLE_ROW_NUMBERS_BUT = "[чисел: {n}, не считая итоговых колонок {columns}]"
GOOGLE_QUESTIONS = "Вопросы:"
GOOGLE_CORRECT = "верный ответ"
GOOGLE_RESPONSES = "Ответов: {n}"
GOOGLE_RESPONSE = "Ответ {n}"
GOOGLE_SCORE = "баллы {score}"
GOOGLE_SIGN_IN = ("⚠️ Google: вход больше не действует — документы Google в ответах не "
                  "участвуют. На компьютере с ботом запустите: "
                  "python -m tools.google_login")
# Reminders (app/reminders/).
REMINDER = "⏰ {text}"
REMINDER_LATE = "⏰ (с опозданием на {delay}) {text}"
REMINDER_MINUTES = "{n} мин"
REMINDER_HOURS = "{n} ч"
REMINDER_DAYS = "{n} дн."
REMIND_SET = "⏰ Напомню {when}: {text}"
REMIND_AT = "{day} в {time}"
REMIND_REPEAT = "{rule}, ближайшее — {when}"
REMIND_WHEN = "⏰ Когда напомнить? Напишите дату или время — «завтра в 10», «в пятницу»."
REMIND_PAST = "⏰ Это время уже прошло: {when}. Напишите другое."
REMIND_NONE = "⏰ Напоминаний нет."
REMIND_LIST = "⏰ Напоминания:"
REMIND_ITEM = "• {when} — {text}"
REMIND_CANCELLED = "⏰ Отменил: {text}"
REMIND_NOT_FOUND = "⏰ Не нашёл такого напоминания."
FIX_REMINDER_KEPT = "⏰ Напоминание осталось как было — если надо, отмените его и поставьте заново."
REMIND_OFF = "⏰ Напоминания здесь не настроены."
# Repeat rules as the reply says them (app/reminders/repeat.py words).
REMIND_EVERY = {"day": "каждый день", "week": "каждую неделю", "month": "каждый месяц",
                "year": "каждый год"}
REMIND_EVERY_N = {"day": "раз в {n} дн.", "week": "раз в {n} нед.", "month": "раз в {n} мес.",
                  "year": "раз в {n} г."}
REMIND_ON_DAYS = "по {days}"
REMIND_DAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# A message that counts from now ("через 2 часа"): its time need not repeat a number.
VAULT_RELATIVE_CUES = ("через",)
# A question about reminders ("какие у меня напоминания") is the remind stage's, not a
# search of the notes; and the words a reminder's text should not start with.
VAULT_REMIND_CUES = ("напомин", "напомни")
REMIND_LEADING = ("про", "о", "об", "обо", "насчёт", "насчет")
REMINDER_RULE_TODAY = "Сегодня: {label}"
REMINDER_RULE_TOMORROW = "Завтра: {label}"
REMINDER_RULE_DAYS = "Через {n} дн.: {label}"
SCOPE_NOT_HERE = "🚫 Здесь так нельзя: {kinds}."
SCOPE_NOTHING = "🤷 Не понял, что записать или найти."
SCOPE_NOT_YOURS = "Это может отменить или поправить только тот, кто об этом просил."
SCOPE_KINDS = {
    "update": "менять или отмечать записанное", "rewrite": "переписывать заметки",
    "move": "переносить", "log": "дневник", "grocery": "продукты", "inbox": "входящие",
    "append": "дописывать в эту заметку", "note": "новые заметки", "task": "новые задачи",
    "search": "вопросы", "agenda": "вопросы", "link_answer": "вопросы",
    "reminder": "напоминания", "reminder_list": "напоминания",
    "reminder_cancel": "напоминания",
}
# Said in place of the Obsidian line when a rewrite was asked for and there is no model to
# do it with.
# The editor read the note and found nothing the instruction applies to. Not a breakage,
# so it must not read like one.
VAULT_NOTHING_TO_CHANGE = "не нашёл, что именно поправить"
VAULT_REWRITE_OFF = "некому переписать — нужен Claude"
# A move the editor could not pin down to lines: nothing is cut, nothing is copied.
# A note that asked for a web search and was written without what it would have found.
# A set the user asked for, every item of which the vault already has.
VAULT_ALL_THERE = "всё это уже есть — ничего не менял"
VAULT_WEB_OFF = "поиск в интернете не подключён, записал без него"
VAULT_WEB_FAILED = "в интернете ничего не нашлось, записал без найденного"
VAULT_WEB_UNCLEAR = "поиску нужен запрос точнее, записал без найденного"
VAULT_NOTHING_TO_MOVE = "не нашёл, что переносить — ничего не тронул"
# Undo found the file changed since, in the very lines it would take back.
VAULT_UNDO_LEFT = "↩️ Не отменил в {notes}: там с тех пор что-то изменили, и я не стал трогать."
# Some of a message's writes failed after others were already on disk.
VAULT_SOME_FAILED = "ещё {n} записать не удалось"
# A note (or the section named) is longer than the editing model may read in one go.
VAULT_TOO_LONG = "заметка слишком длинная, чтобы менять её целиком — назовите раздел"
VAULT_MOVE_UNCLEAR = "не понял, какие строки переносить — ничего не тронул"
# Fixing part of what was just written (app/vault/fix.py).
FIX_ASK = "✏️ Что поправить? Напишите или надиктуйте."
FIX_EXPIRED = "Поправить уже нельзя (прошло больше {minutes} минут)."
FIX_UNCLEAR = "не понял, что поправить — ничего не менял"
FIX_DONE = "✏️ Obsidian — поправлено: {what}"
FIX_FAILED = "⚠️ Obsidian — не поправил: {error}"
FIX_MOVED = "«{item}» → «{place}»"
FIX_SET = "«{item}» — {value}"
FIX_DROPPED = "«{item}» убрано"
FIX_ADDED = "+ «{item}»"
FIX_NOT_MOVED = "«{item}» не перенёс"
FIX_NOTE = "«{note}» ещё раз"
# New writes of a fix that failed: their words, so the user can send them again.
FIX_NOT_WRITTEN = "не записал: {items}"
# The old lines a fix could not take back (the file was held by another program): the new ones
# are written anyway, so the old ones are there twice until removed by hand.
FIX_NOT_TAKEN_BACK = "старое осталось в {notes} — уберите руками"
# A fix may drop something only when the correction says to: whole words, and word starts.
# Found live: «ну это» made the model drop the task it was shown.
FIX_DROP_WORDS = ("нет", "без", "ненадо")
# «не» drops only before these word starts («не надо», «не нужно»): «не в задачи, а в продукты»
# is a move, not a drop.
FIX_DROP_AFTER_NE_WORD = "не"
FIX_DROP_AFTER_NE = ("надо", "нужн")
FIX_DROP_STEMS = ("убер", "убра", "удал", "лишн", "отмен", "ненуж", "выкин", "вычеркн")
VAULT_WHAT = {
    "task": "задача в «{note}»",
    "note": "заметка «{note}»",
    "append": "дописано в «{note}»",
    "update": "обновлено «{note}»",
    "rewrite": "переписано «{note}»",
    "log": "запись в дневнике «{note}»",
    "inbox": "в «{note}» — не понял, куда это",
    # The model could not be asked at all: the words are kept as they are.
    "kept": "сохранил в «{note}» как есть",
    "grocery": "в список продуктов: {detail}",
    "grocery_done": "куплено: {detail}",
}
# The same, when the writer said what exactly it wrote (app/vault/summary.py builds {detail}).
VAULT_WHAT_DETAIL = {
    "task": "задача {detail}",
    "note": "заметка {detail}",
    "append": "дописано в «{note}»{detail}",
    "update": "обновлено «{note}»{detail}",
    "log": "в дневник: {detail}",
    "inbox": "в «{note}» — не понял, куда это: {detail}",
}
# A reply with several writes lists them, one per line, under this.
VAULT_REPLY_LIST = "✅ Obsidian:"
VAULT_LIST_ITEM = "• {what}"
VAULT_LIST_MORE = "• … и ещё {n}"
VAULT_LIST_NOTE = "⚠️ {notes}"
VAULT_SUM_TODAY = "сегодня"
VAULT_SUM_TOMORROW = "завтра"
VAULT_WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
VAULT_SUM_NOTE = "«{name}» в «{folder}»"
VAULT_SUM_REOPENED = "{task} снова открыта"
VAULT_SUM_CLEARED = "пусто"

# ---- the vault's agenda: the morning message and the "what now" answer -------------------------

VAULT_AGENDA_HEADER = "🌅 {date} — что на подходе:"
VAULT_AGENDA_OVERDUE = "🔴 Просрочено ({n}):"
VAULT_AGENDA_TODAY = "📌 Сегодня ({n}):"
VAULT_AGENDA_TOMORROW = "➡️ Завтра ({n}):"
VAULT_AGENDA_EVENTS = "📅 Встречи и даты ({n}):"
VAULT_AGENDA_ITEM = "• {text}"
VAULT_AGENDA_MORE = " и ещё {n}"
# Answers to a question about a day or a range, and to "что мне сейчас делать".
VAULT_ON_DAY = "📅 {date}:"
VAULT_ON_RANGE = "📅 {start} — {end}:"
VAULT_NOW = "С чего начать:"
VAULT_OVERDUE = "🔴 Просрочено:"

# ---- the bot's own name -----------------------------------------------------------------------

# Questions about the bot itself, answered without a model (app/address.py).
ABOUT_QUESTIONS = ("кто ты", "ты кто", "как тебя зовут", "твоё имя", "твое имя",
                   "что ты умеешь", "что умеешь", "чем помочь", "что ты можешь")
# The answer to those, and to being called by name with nothing else in the message.
ABOUT = ("Меня зовут {name}. Записываю в Obsidian: задачи со сроками и повторами, "
         "заметки, книги, дневник. Отвечаю на вопросы: «что у меня в пятницу», «что сейчас "
         "делать», «что там про борщ». Утром в {digest_at} присылаю сводку. Любую запись можно "
         "отменить кнопкой.")
CALLED = "Да, слушаю."
# Words that may come before the name when someone calls out to the bot ("эй Робо", "ну Робо").
ADDRESS_OPENERS = ("эй", "ей", "hey", "ok", "окей", "ну", "слушай", "привет")

# ---- mail digest ------------------------------------------------------------------------------

MAIL_MARKED = "Отмечено прочитанными: {n}"
MAIL_UNMARKED = "Снова непрочитанные: {n}"
MAIL_MARK_GONE = "Этот дайджест слишком старый — отметьте письма в почте"
MAIL_MARK_FAILED = "Не получилось: {error}"
MAIL_HEADER = "📬 Почта — {n} писем:"
MAIL_BUCKET = "\n{bucket} ({n}):"
MAIL_ITEM = "• {sender} — {summary}"
MAIL_MORE = "  …и ещё {n}"
MAIL_FAILED = "📬 Почту прочитать не удалось ({error})."

# The second digest: the same mail sorted by the local model, for comparison. Temporary — it
# goes away once the user has decided whether the local model is good enough.
MAIL_SHADOW_HEADER = "🧪 Тот же разбор локальной моделью ({model}, {seconds:.0f} с):"
MAIL_SHADOW_FAILED = "не получилось — {error}"
MAIL_SHADOW_EMPTY = "ничего не вернула."
# How a reply quotes what came before it ("25.09.2026 Иван написал:"): everything from there
# down is the previous message again, so the classifier never sees it.
MAIL_QUOTE_MARKERS = ("написал", "wrote", "schrieb", "kirjutas")
# The buckets a digest is sorted into, and what belongs in each. The user's own MAIL_BUCKETS
# replaces this wholesale; the descriptions are what the classifier follows, not the names.
MAIL_BUCKETS_DEFAULT = (
    "bills: счёт или требование оплаты — что-то нужно оплатить\n"
    "shopping: магазины и заказы — статус заказа, доставка, акции, отзывы\n"
    "financial: банк и платежи — чеки об оплате, уведомления и напоминания банка\n"
    "notifications: автоматические уведомления сервисов, рассылки, новости\n"
    "personal: письмо от живого человека лично тебе\n"
    "other: всё остальное"
)

# ---- Syncthing health (app/watch/syncthing.py) ------------------------------------------------

SYNC_PC_DOWN = "⚠️ Syncthing на этом компьютере не отвечает — хранилище не синхронизируется."
SYNC_PC_BACK = "✅ Syncthing на этом компьютере снова работает."
SYNC_DEVICE_GONE = ("⚠️ Syncthing: «{name}» не на связи больше {minutes} мин "
                    "(последний раз — {seen}). Изменения оттуда сюда не доходят.")
SYNC_DEVICE_BACK = "✅ Syncthing: «{name}» снова на связи."
SYNC_FOLDER_ERRORS = "⚠️ Syncthing: в папке «{name}» ошибки синхронизации ({n})."
SYNC_FOLDER_OK = "✅ Syncthing: папка «{name}» снова без ошибок."
SYNC_FOLDER_STOPPED = "⚠️ Syncthing остановил папку «{name}»: {error}."
SYNC_STOPPED = "остановлена: {error}"
# The admin page's status rows.
SYNC_THIS_PC = "Этот компьютер"
SYNC_RUNNING = "работает"
SYNC_NOT_RUNNING = "не отвечает"
SYNC_CONNECTED = "на связи"
SYNC_PAUSED = "на паузе"
SYNC_LAST_SEEN = "нет связи, последний раз — {seen}"
SYNC_ERRORS = "ошибок: {n}"
SYNC_IN_ORDER = "в порядке"

# ---- Watched folders (app/watch/folders.py) --------------------------------------------------

FOLDER_OK = "✅ {title}: всё в порядке (новых файлов: {n})."
FOLDER_ATTENTION = "📂 {title}:\n{text}"
FOLDER_CUT = "[…дальше файл обрезан]"
# The admin page's status line per folder.
FOLDER_MISSING = "папки нет или она недоступна"
FOLDER_MODEL_FAILED = "не удалось прочитать моделью — попробую в следующий раз"
FOLDER_SEEN = "файлов: {n}, последний изменён {last}"
FOLDER_NOT_YET = "ещё не проверялась"

# A plan with a write part that failed: the message is kept whole in the inbox note.
PLAN_KEPT = f"Сообщение целиком сохранено в «{VAULT_INBOX_NOTE}»."

# A bare Instagram link in a private chat whose post could not be fetched (app/telegram/media.py).
INSTAGRAM_NOT_FETCHED = "🤷 Не получилось достать пост из Instagram."
