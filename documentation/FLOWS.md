# Flows

All bot replies in Russian. Buttons in `[brackets]`.

## F1. Task, unambiguous

1. User (voice): «позвонить врачу завтра в 10»
2. Transcript stored in audit (not shown).
3. Index refreshed. Staged reader: intent `add`; target: the task file; details: the words, the date and the time.
4. `_Gate`: the message names a time, so the date stands. `filer.check` passes the action.
5. The writer adds one line to the task file: `- [ ] 10:00 позвонить врачу 📅 2026-11-03`.
6. Bot (`VAULT_REPLY` around `VAULT_WHAT_DETAIL["task"]`; the detail comes from `summary.task`, built from the action as written — no model is asked to describe its own work):
   ```
   ✅ Obsidian — задача «позвонить врачу» · 📅 завтра 10:00
   ```
   `[Отменить] [Поправить]`.
7. `[Отменить]` within 5 min → the line is taken back → `↩️ Отменено.`
8. Behind the reply the linker adds links to existing notes.

## F2. New note in a folder

1. «добавь книгу Солярис, автор Лем»
2. Staged reader: intent `add`; target: the folder Книги; details: a schema built from the properties that folder's notes share, so the answer has the shape of the folder.
3. The writer makes `Книги/Солярис.md`, with the properties in the order the folder's newest note has them and a link where the folder keeps that property as one (`VaultWriter._shaped`). A property the message did not name stays an empty `key:`: nothing is asked for.
4. Bot:
   ```
   ✅ Obsidian — заметка «Солярис» в «Книги» · Лем
   ```
   `[Отменить] [Поправить]`.
5. `[Отменить]` → the note goes to `.trash`, unless something else was written into it meanwhile.
6. The note is already there and the message only sets properties: `filer.check` turns it into an `update` of that note rather than a copy with " 2" on its name.

## F3. Append to a note

1. «в идеи: попробовать сыр с плесенью»
2. Staged reader: intent `add`; target: the note the message names; details: the lines to add, and a section when one fits.
3. The writer adds the line to the note, under that section or at its end.
4. Bot (`summary.lines`: the first line added, and how many more came with it):
   ```
   ✅ Obsidian — дописано в «Идеи»: «попробовать сыр с плесенью»
   ```
   `[Отменить] [Поправить]`.

## F4. Tick a task off

1. «забрал посылку»
2. The reader is shown the open tasks that share a word with the message. Intent `done`; the action is an `update` of that line.
3. The writer ticks the line, leaving everything else on it alone.
4. Bot:
   ```
   ✅ Obsidian — обновлено «Задачи»: ✓ «забрать посылку»
   ```
   `[Отменить] [Поправить]`.
5. The product is on the grocery page: `filer.check` makes it a `grocery` write instead (F23). A re-date of a task the file does not have becomes that new task (`filer._or_new_task`); something ticked off that is not there never adds an open one.

## F5. Не понял → в разное

1. A dictated message nothing can be made of.
2. Intent `unclear`, or the reader answered nothing usable, or it named a note that is not there: `filer.check` makes it an `inbox` action holding the user's own words.
3. Bot:
   ```
   ✅ Obsidian — в «Разное» — не понял, куда это: «…»
   ```
   `[Отменить] [Поправить]` — it is a write like any other, so «это в задачи» moves it (F7).
4. The model could not be asked at all: the words are kept the same way, and the reply says why (F22).
5. In a group there is no inbox: `🤷 Не понял, что записать или найти.`, and nothing is written (F10).

## F6. A value with no source

1. «сделать отчёт» — no date in the message.
2. Asked for a date, a model tends to supply one. `_Gate.date` counts it only when the message has a digit or a word that names a time, so it is dropped here.
3. The task is written without a date:
   ```
   ✅ Obsidian — задача «сделать отчёт»
   ```
   `[Отменить] [Поправить]`. «на пятницу» then adds it (F7).
4. The same goes for a property value or a title whose words are in neither the message, the guide nor the linked pages. Nothing is asked: the value simply is not written.

## F7. Fix part of a write

1. The bot wrote `✅ Obsidian — задача «кефир»`.
2. `[Поправить]` → the keyboard goes away, bot: `✏️ Что поправить? Напишите или надиктуйте.` A Telegram reply to the write's message skips this step: the reply is itself the correction, and it outranks a pending press.
3. User: «не в задачи, а в продукты».
4. One call to the filer model sees the written actions as keyed lines, the place list and the correction, and answers drop / set / move / add. Values pass the same gate as a fresh message (F6); a drop needs a word of the user's that asks for it.
5. Code takes back the changed writes and writes the new ones as one step (`VaultWriter.replace_writes`). Bot:
   ```
   ✏️ Obsidian — поправлено: «кефир» → «Продукты»
   ```
   `[Отменить] [Поправить]`. The new row's Undo goes back to before the first message.
6. Not understood: `⚠️ Obsidian — не поправил: не понял, что поправить — ничего не менял`, with the buttons back. The old row stays valid.
7. A line the fix would take back was edited by hand: nothing at all is touched, and the reply names the note.
8. Past the undo window: `Поправить уже нельзя (прошло больше 5 минут).` That is the answer while the press itself is younger than the window, so «в продукты» is not filed as a grocery line. An older press is forgotten, and the message is read as a new one.

## F8. Undo

1. `[Отменить]`, or `/undo` — the latest write in this chat; in a group, the sender's own latest.
2. The row must be this chat's, not undone and not expired, otherwise: `Отменить уже нельзя (прошло больше 5 минут).`
3. Reminders the turn set are cancelled first, whatever then happens to the files.
4. Every write of the turn is taken back, newest first, out of whatever the file holds by then (`app/vault/revert.py`): a task added by the next message, or the links the linker put in, stay. Bot: `↩️ Отменено.`
5. A file whose lines were changed since is left alone: `↩️ Не отменил в «…»: там с тех пор что-то изменили, и я не стал трогать.` When every write was left alone the row stays, so the user can revert their edit and send `/undo` again.
6. The undo itself raised: `Не удалось отменить: <exception class>.`
7. A double-tapped button is two callbacks ~100 ms apart: the chat's lock serialises them, and the second finds the row undone.

## F9. Reminder

1. «напомни завтра в 10 позвонить врачу»
2. Staged reader: intent `remind`; `REMIND_PROMPT` returns an add, which becomes a `reminder` action plus a `task` with the same words, date, time and 🔁.
3. The pipeline (`_reminded`) completes the moment (`timing.first_due`), stores it and dates the task. Bot:
   ```
   ✅ Obsidian — задача «позвонить врачу» · 📅 завтра 10:00
   ⏰ Напомню завтра в 10:00: позвонить врачу
   ```
   `[Отменить] [Поправить]`.
4. At that moment the bot sends `⏰ позвонить врачу` to this chat.
5. `[Отменить]` cancels the reminder and takes the task back. A fix leaves the reminder as it was and says so.
6. No time at all: `⏰ Когда напомнить? Напишите дату или время — «завтра в 10», «в пятницу».` A moment already past: `⏰ Это время уже прошло: …. Напишите другое.` Either way nothing is set, the task is dropped with it, and nothing waits for the answer.
7. «какие у меня напоминания», «отмени напоминание про хлеб»: listed or cancelled, nothing written.

## F10. Group chat

1. A group listed in `data/chats.json`. A message that does not address the bot (@mention, a reply to the bot, its name, a command) costs nothing.
2. Addressed: the message goes through `VaultPipeline.scoped(scope)`. The readers see only the notes in scope, `gate()` holds every action to the chat's rights, and the writer refuses any other file.
3. A task always gets the chat's tag, and a new note goes to the home folder. Diary, groceries and the inbox never.
4. A right the chat does not have: `🚫 Здесь так нельзя: …`. Something else in the same message is still done, with that line beside it.
5. `[Отменить]` and `[Поправить]` work for the member who asked and for an owner. Anyone else is told so in a notice only they see (`Это может отменить или поправить только тот, кто об этом просил.`), and the message keeps its buttons.

## F11. Web search and write

1. «найди рецепт борща и запиши в заметки»
2. The details stage names a web search. It counts only when the message has one of the user's search words (`Tuning.web_words`).
3. The pipeline runs `WebResearcher.research` and the Markdown it returns becomes the note's body as it stands, never entering a prompt.
4. Bot, as in F2; the Undo window starts when the note is written, not when the message arrived.
5. A search that fails still leaves the note the user asked for: `✅ Obsidian — заметка «…» в «Заметки» — в интернете ничего не нашлось, записал без найденного`.
6. «найди рецепт борща» with nowhere to write it is a question: the answerer goes to the web, `🔎 Ищу в интернете…` while it runs, then `🌐 …`, and nothing is written.

## F12. Link in a message

1. «в список книг https://…»
2. `LinkReader` reads up to three links in parallel, for at most 12 seconds. A site on the never-open list is not read; either way the link stays a plain link.
3. The page goes to the reader beside the message, under a header that calls it data. The details stage uses it for the action it picks: here, the book's real title.
4. Bot, as in F2 or F3.
5. «сколько тут этажей? https://…»: the question stage returns `link_answer`, and `LinkAnswerer` replies from the page. Nothing is written.
6. That answer failed: `Не получилось ответить по ссылке — попробуйте спросить ещё раз.`

## F13. Voice transcription failure

1. Empty or unintelligible audio → transcript empty.
2. Bot: `Не разобрал голосовое сообщение. Повторите или напишите текстом.` No LLM call, and no `events` row: the handler answers before the orchestrator is called.

## F14. Infrastructure errors

| Failure | Bot reply | Audit |
|---|---|---|
| Claude refused or unreachable while reading | the words kept in the inbox note, `✅ Obsidian — сохранил в «Разное» как есть — <why>` (see F22) | `error` of the call in `llm_response` |
| A file could not be written | `⚠️ Obsidian — не записано: <exception class>`; when other writes went through, they are listed with `ещё N записать не удалось` | as above |
| The vault cannot be read | `⚠️ Obsidian — не записано: <exception class>` | as above |
| A question's answerer failed | the list of what the search found | logged |
| The vault raised something nobody expected | `Не удалось обработать сообщение.` | `INTERNAL` |
| The audit database is locked or unwritable | `Не удалось обработать сообщение.` | logged: there is no row to write to |
| Telegram unreachable | none: polling retries on its own, one line a minute in the log | logged |

## F15. Admin flow

1. Open `http://127.0.0.1:8787` → the settings page, one collapsible section per area.
2. Edit and save → `POST /api/settings` writes the files in `data/` that changed.
3. The next message, or the next run of a digest or a watcher, reads them. No restart.
4. Telegram: `/undo` → undo the last write if within the window. `/start`, `/help` → usage text.

## F16. Unauthorized user

Any update from a user id outside `TELEGRAM_ALLOWED_USER_IDS` is ignored (no reply) and logged at WARNING without message content.

## F17. Вопрос к хранилищу Obsidian

Пользователь: «что у меня по дому?»

1. Читатель возвращает действие "search" (слова, тэги, папка, точные свойства) — ничего не пишется.
2. `app/vault/search.py` ищет по индексу и самим заметкам, без обращения к модели: сначала
   совпадения по названиям и алиасам, затем свойства и текст; открытые галочки отвечают
   задачами. Если фильтр (папка/тэг/свойство) задан, а слова не нашлись — перечисляется то, что
   прошло фильтр (заметки с датой в названии — новые сверху).
3. Найденное читает `VaultAnswerer` (`VAULT_ANSWER_MODEL`) и отвечает: `🔍 …` по заметкам,
   `💬 …` из общих знаний, `🌐 …` после поиска в интернете. Спорный ответ о записях владельца
   перечитывает `ESCALATE_MODEL`.
4. Без этой модели, или если она не ответила, в ответе — строки вида
   `• [Название](obsidian://open?...)`: ссылка открывает заметку в Obsidian, в том числе на
   телефоне. Ничего не найдено — «🔍 Obsidian — ничего не нашёл.»
5. Вопрос о себе, а в записях непонятно, кто спрашивает: `❓ Как вы записаны? Напишите своё имя
   так, как оно там.` Следующее короткое сообщение — это имя; оно запоминается, и на первый вопрос
   бот отвечает сразу.

## F18. Утренняя сводка и вопросы про даты

1. В 09:00 (TIMEZONE, настройка DAILY_DIGEST_AT) бот сам присылает: просрочено, сегодня,
   завтра, встречи на эти дни. Если ничего нет — не присылает ничего.
2. «что у меня в пятницу?», «какие планы на следующую неделю?» → читатель отвечает действием
   "agenda" с диапазоном дат; ответ собирается из файлов хранилища, без второго вызова модели.
3. «что мне сейчас делать?», «что я пропустил?» → "agenda" со scope=now: сначала пропущенные
   разовые задачи, потом просроченные повторяющиеся, потом сегодняшние, потом дела без срока —
   не больше пяти строк.
4. Даты берутся из строк задач (📅) и из свойств заметок (`date`, `дата`, …), поэтому встречи
   клуба попадают в ответ наравне с задачами.

## F19. Переписать то, что уже написано

Пользователь: «в заметке про шведскую стенку оставь один подход и одни размеры»

1. Читатель отвечает действием `rewrite`: заметка, при желании раздел, и указание пользователя
   своими словами. Готовый текст он не пишет — содержимого заметки он не видит.
2. Текущий текст читается уже при выполнении: вся заметка или раздел, без свойств.
3. Один вызов Sonnet (`app/llm/edits.py`) возвращает список правок или весь новый текст (F20).
4. Писатель заменяет текст, свойства заметки остаются на месте.
5. Прежняя версия дополнительно кладётся в `.trash` и переживает окно отмены.
6. В ответе: `✅ Obsidian — переписано «…»` и кнопки `[Отменить] [Поправить]`.

Нечего менять («не нашёл, что именно поправить»), заметка слишком длинная, нет Claude — сообщение
об этом, и ничего не изменено. Названного раздела больше нет — правка относится ко всей заметке.

## F20. Правка отдельного места в заметке (и картинки)

Пользователь: «убери второй вариант», «поменяй высоту на 240», «убери картинку»,
«добавь картинку вот по этой ссылке»

1. Читатель отвечает действием `rewrite`: заметка и указание словами пользователя. Содержимого
   заметки он по-прежнему не видит — оно читается только на шаге 2.
2. Заметка (или названный раздел) читается один раз и показывается модели построчно, с номерами.
   Картинка — такая же строка, как остальные.
3. Один вызов Sonnet возвращает либо список правок (`replace` / `insert` / `delete` / `image`),
   либо — если просят переписать всё — весь новый текст. Модель выбирает сама.
4. **Что применить, решает код** (`app/vault/mdedit.py: apply_edits`): правка с номером строки,
   которой нет, просто игнорируется — модель может указать только на то, что ей показали.
5. Правки не сдвигают друг друга: каждая отнесена к той нумерации, которую видела модель. Замены
   идут на месте, вставки собираются по строкам, удаления только убирают строки.
6. Картинку можно удалить — об этом просили: удаляется строка, сам файл остаётся. Картинка по
   ссылке добавляется строкой `![подпись](ссылка)`.
7. Отмена убирает именно эту правку; прежняя версия заметки лежит в `.trash`.

Если править нечего («не нашёл, что именно поправить») — об этом сказано, и ничего не изменено.

## F21. Сообщение из нескольких шагов

Пользователь: «найди про шведские стенки и допиши в заметку Проекты, потом найди про маты и сравни»

1. Рядом с обычным чтением — один вопрос лёгкой модели: можно ли сделать это одним шагом? Да —
   обычный путь, никто никого не ждёт.
2. Нет — чтение отбрасывается, и сильная модель возвращает связанные шаги: `write`, `ask`, `read`,
   `research`, `derive`. Каждый — самостоятельный запрос, с `needs` и `reply`.
3. Шаги идут раундами: в раунде те, кому уже хватает результатов предыдущих, и выполняются вместе.
4. Текст, который шаг получил (результат веб-поиска), пишется как есть: один поиск, один счёт. В
   промпт этот текст не попадает — то, что вернул интернет, это данные.
5. Куда положить — решает читатель по своему индексу. Шаг с вопросом пользуется результатами как
   данными.
6. Всё сливается в один ход: один ответ и одна кнопка «Отменить», которая откатывает все записи
   сообщения.
7. Шаг, который не удался, называется в ответе и пропускает только то, что от него зависит. Если
   не удалась запись, исходное сообщение один раз уходит в «Разное».

Пределы: 8 шагов, 3 поиска на сообщение.

## F22. Claude недоступен

1. Любой отказ Anthropic API опознаётся в `app/llm/health.py`: кончились кредиты, ключ не
   принят, слишком много запросов, сервис не отвечает.
2. Запасной модели нет: сообщение нечем прочитать, поэтому его слова сохраняются как есть:
   `✅ Obsidian — сохранил в «Разное» как есть — закончились кредиты Claude`.
3. К ответу добавляется одна понятная строка — не чаще раза в час:
   «⚠️ На счёте Anthropic закончились кредиты… Пополнить: console.anthropic.com/settings/billing».
4. Строка Obsidian называет причину словами, а не `claude 400`.
5. Утренняя сводка и почтовый дайджест несут ту же строку: сводка, которая молча не собралась,
   хуже той, что объясняет почему.
6. Причина держится, пока вызов не пройдёт. Сбой старше пяти минут перед новым предупреждением
   проверяется одним коротким вызовом, чтобы не повторять то, что уже исправлено.

Без ключа совсем Obsidian-сторона не собирается: при запуске в журнале предупреждение, а на
сообщение бот отвечает `NOTHING_ENABLED`.

## F23. Продукты: реестр вместо списка дел

Страница «Продукты» — это реестр: у каждого продукта одна строка навсегда. Галочка стоит — есть
дома, снята — надо купить. Ничего не создаётся и не архивируется.

1. «надо яйца и молока» → бот находит строки по названию (окончания и количества не мешают:
   «2 л молока» — это «молоко») и снимает галочки. Незнакомый продукт получает новую строку, в
   алфавитном порядке — так реестр из ста строк остаётся читаемым.
2. На Главной они появляются сами: запрос `not done` + `filename includes Продукты`. Тэга на
   строках нет — источник определяет файл.
3. В магазине галочки ставятся прямо с Главной. Файл при этом не растёт: строки те же.
4. «купил яйца» делает то же самое словами. «что надо купить?» — просто перечисляет список,
   ничего не записывая.
5. **Что считается продуктом, решает сама страница, а не модель.** Если название уже есть в
   реестре — это продукт, чем бы модель ни ответила. Модель нужна только для слова, которого на
   странице ещё не было.
6. Ошибку исправляет пользователь: удалил строку со страницы — и бот больше никогда не отнесёт
   это к продуктам; добавил — наоборот. Реестр и есть способ научить бота.
7. Техника, одежда, подарки — обычные задачи: их покупают один раз, и архивировать их правильно.
   Покупка со сроком («торт к субботе») — тоже задача, потому что у строки продукта не бывает
   даты.
8. У продуктов не бывает `🔁` и `📅`: повторяющаяся галочка при закрытии создаёт свою копию —
   именно так в архиве задач появляются одинаковые строки-двойники.
9. В списках дел («что мне сейчас делать», утренняя сводка) продукты не участвуют: в сводку
   попадает одна строка «🛒 Продукты (3): …», и только если покупать действительно есть что.
