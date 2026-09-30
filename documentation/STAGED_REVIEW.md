# Staged reader vs today's reader — cases to judge

All 81 real messages from the audit log, each read by both readers with the vault context frozen for that message (folder and page shapes read from the vault as it is now). Model under both: claude-haiku-4-5.

- They do the same thing on **31** and differ on **50**.
- Tokens per message, input + output: single 4458 + 163, staged 3985 + 82.
- Note: the contexts were frozen from the vault as it is *now*, so a task the message originally created is already listed as open. `update` with no `done` is a reader noticing that duplicate and pointing at the existing line; `update … done=true` ticks it off.

## Pre-sorted (Claude's reading — overrule anything)

**Staged is right — 28.** No need to look unless you disagree.

- The single reader ticks an existing task *done* on a message that only says it has to be done, or on a one-word reply; the staged one files or points at the task, or keeps a fragment in the inbox: e1, e11, e43, e46, e49, e50, e53, e56, e60, e61, e70, e71, e72, e74, e80, e83, e85, e86, e106, e108.
- Fragments kept in the inbox instead of becoming tasks or a book called «Достоевский»: e22, e29.
- Films and series go to the list on «медиа», as `_bot.md` says: e23, e32, e35, e37, e38.
- The move names the right source: e96.

**Your call — 22.** These are about what you want, not about which reader parsed the sentence.

- *Should the vault write things the model knows but you did not say?* The staged reader refuses (a title or author has to be in your message), so «добавь все романы Пелевина» gives one note instead of eight: e16, e18, e27, e31.
- *«Надо/нужно посмотреть фильм» — task or the media list?* e24, e26.
- *Requests that need a web search or an edit of a page* («найди … и запиши», «добавь картинок к странице»): the vault gets the found text from the Notion side afterwards, so what matters here is only where it lands: e6, e7, e8, e10, e12, e21, e45, e90, e93.
- *Note, task, or a line on the area's page?* e54, e65, e81, e84, e109.
- *Append vs rewrite of the same note* (the text reaches the note either way): e94.
- *Already-open task, garbled dictation* — the single reader recognised it, the staged one kept the words in the inbox: e69.

Mark each below: **1** single is right, **2** staged is right, **0** neither, **=** both fine.

### e1 — «надо забрать посылки»
- single:
  - update note="Задачи" task="Забрать посылку из кауп #personal" done=true
- staged:
  - task text="забрать посылки"
- verdict: 

### e6 — «Найди, пожалуйста, рецепт борща из копченой курицы и запиши его на страницу домашнюю, ну, в смысле, дом.»
- single:
  - search folder="Области" text="рецепт борща из копченой курицы"
- staged:
  - append note="дом" body=["рецепт борща из копченой курицы"]
- verdict: 

### e7 — «Добавь ещё картинок для референса к странице с борщом, пожалуйста, чтобы было красиво там на странице.»
- single:
  - inbox text="Добавь ещё картинок для референса к странице с борщом, пожалуйста, чтобы было красиво там на странице."
- staged:
  - note folder="Заметки" title="картинки для референса к странице с борщом"
- verdict: 

### e8 — «Не найди картинки референсов к каждому шагу приготовления борща.»
- single:
  - search text="картинки референсы приготовление борща"
- staged:
  - task text="найти картинки референсов к каждому шагу приготовления борща"
- verdict: 

### e10 — «Найди мне информацию про японские ворота Тории и вместе с изображениями, референсами создай мне страницу в проектах, чтобы я мог увидеть разные виды ворот и как их делать.»
- single:
  - search text="японские ворота Тории"
  - note folder="Заметки" title="Тории — виды и техники" props={"category": "пройекты"}
- staged:
  - note folder="Области" title="Японские ворота Тории" body=["Информация про японские ворота Тории", "Разные виды ворот", "Как их делать"]
- verdict: 

### e11 — «Надо навести порядок в мастерской.»
- single:
  - update note="Задачи" task="Навести порядок в мастерской #gnezdo" done=true
- staged:
  - update note="Задачи" task="Навести порядок в мастерской #gnezdo"
- verdict: 

### e12 — «Планируй мне поездку во Вьетнам на неделю и создай страницу для этого под страницей «Моя херня» и там сделай список городов, которые надо посетить, и картинки, референсы, и составь маршрут с коротким описанием, что делать в каждом городе. Один-два дня, ну, два дня давай, и картинки достопримечательностей.»
- single:
  - note folder="Заметки" title="Вьетнам" props={"parent": "моя херня"} tags=["personal"]
- staged:
  - note folder="Области" title="Поездка во Вьетнам" body=["Список городов для посещения", "Референсы и картинки достопримечательностей", "Маршрут с описанием", "По 1-2 дня в каждом городе"] props={"tag": "personal"} tags=["personal"]
- verdict: 

### e16 — «я прочитал все романы Пелевина можешь пожалуйста найти их и добавить в список прочитанных книг»
- single:
  - search folder="Книги" text="Пелевин"
- staged:
  - note folder="Книги" title="Романы Пелевина" props={"status": "Read", "author": "Пелевин"}
- verdict: 

### e18 — «я прочитал все романы Пелевина пожалуйста добавь их в список прочитанных книг в порядке написания и возьми пожалуйста самый последний весь»
- single:
  - note folder="Книги" title="Generation П" props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="Жизнь насекомых" props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="Святая книга оборотня" props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="t" props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="Числа" props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="Снафф" props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="S.N.U.F.F." props={"status": "Read", "author": "Виктор Пелевин"}
  - note folder="Книги" title="Неvotaм" props={"status": "Read", "author": "Виктор Пелевин"}
- staged:
  - note folder="Книги" title="Чапаев и Пустота" props={"status": "Read"}
- verdict: 

### e21 — «Мы живём в Таллине и мы хотим куда-нибудь на выходные съездить сплавать на пароме хельсинки или Стокгольм и у нас маленький ребёнок и пожалуйста сравнить варианты и придумай нам план Куда сходить лучше в октябре И что там поделать»
- single:
  - note folder="Заметки" title="Выездка на выходные: Хельсинки или Стокгольм" body=["Живём в Таллине, маленький ребёнок", "Хотим съездить на выходные на пароме", "Варианты: Хельсинки или Стокгольм", "Нужно сравнить варианты", "Нужен план на октябрь", "Что там поделать с ребёнком"] tags=["personal"]
- staged:
  - search text="выходные Таллин паром Хельсинки Стокгольм маленький ребёнок" tags=["personal"]
- verdict: 

### e22 — «1. вторая половина октября. 2. сравнительный план. 3. ребёнку 1.5 года, активности любые»
- single:
  - task heading="knub" text="вторая половина октября" due="2026-10-15" tags=["knub"]
  - task heading="knub" text="сравнительный план" tags=["knub"]
  - task heading="gnezdo" text="ребёнку 1.5 года, активности любые" tags=["gnezdo"]
- staged:
  - inbox text="1. вторая половина октября.\n2. сравнительный план.\n3. ребёнку 1.5 года, активности любые"
- verdict: 

### e23 — «Смотреть кино надо Uncharted»
- single:
  - note folder="Заметки" title="Uncharted" props={"status": "To watch"}
- staged:
  - append note="медиа" heading="смотреть" body=["кино надо Uncharted"]
- verdict: 

### e24 — «Нужно посмотреть фильм Uncharted»
- single:
  - note folder="Заметки" title="Uncharted" heading="Фильмы" body=["Uncharted"]
- staged:
  - task heading="медиа" text="посмотреть фильм Uncharted"
- verdict: 

### e26 — «Надо посмотреть фильм Uncharted»
- single:
  - note folder="Заметки" title="Uncharted" heading="медиа" props={"status": "To watch"}
- staged:
  - task text="посмотреть фильм Uncharted"
- verdict: 

### e27 — «Я хочу прочитать все книги Достоевского но некоторые книги Я уже прочитал Добавь пожалуйста все романы которые ты найдёшь Достоевского в список книг что я хочу их прочитать Но менять что я уже прочитал преступление и наказание идиот вон И всё И сейчас я читаю Братья Карамазовы»
- single:
  - note folder="Книги" title="Бесы" props={"status": "To read", "author": "Федор Достоевский"}
  - note folder="Книги" title="Бедные люди" props={"status": "To read", "author": "Федор Достоевский"}
  - note folder="Книги" title="Униженные и оскорблённые" props={"status": "To read", "author": "Федор Достоевский"}
  - note folder="Книги" title="Записки из мёртвого дома" props={"status": "To read", "author": "Федор Достоевский"}
  - note folder="Книги" title="Записки из подполья" props={"status": "To read", "author": "Федор Достоевский"}
  - note folder="Книги" title="Игрок" props={"status": "To read", "author": "Федор Достоевский"}
  - note folder="Книги" title="Демоны" props={"status": "To read", "author": "Федор Достоевский"}
  - update note="Преступление и наказание" props={"status": "Read"}
  - update note="Идиот" props={"status": "Read"}
  - update note="Братья Карамазовы" props={"status": "Reading"}
- staged:
  - note folder="Книги" title="Преступление и наказание" props={"author": "Достоевский", "status": "Read"}
  - note folder="Книги" title="Идиот" props={"author": "Достоевский", "status": "Read"}
  - note folder="Книги" title="Братья Карамазовы" props={"author": "Достоевский", "status": "Reading"}
  - note folder="Книги" title="Униженные и оскорблённые" props={"author": "Достоевский", "status": "To read"}
- verdict: 

### e29 — «Достоевский»
- single:
  - note folder="Книги" title="Достоевский" props={"status": "To read"}
- staged:
  - inbox text="Достоевский"
- verdict: 

### e31 — «Я хочу прочитать все романы Достоевского Пожалуйста найди в интернете и добавь в список книг которые я хочу прочитать но некоторые книги Я уже прочитал например Преступление и наказание идиот а Братья Карамазовы я сейчас читаю»
- single:
  - update note="Преступление и наказание" props={"status": "Read"}
  - update note="Идиот" props={"status": "Read"}
  - update note="Братья Карамазовы" props={"status": "Reading"}
  - note folder="Книги" title="Демоны" props={"status": "To read", "author": "Достоевский"}
  - note folder="Книги" title="Бесы" props={"status": "To read", "author": "Достоевский"}
  - note folder="Книги" title="Подросток" props={"status": "To read", "author": "Достоевский"}
  - note folder="Книги" title="Униженные и оскорблённые" props={"status": "To read", "author": "Достоевский"}
- staged:
  - append note="медиа" heading="читать" body=["Преступление и наказание", "Идиот", "Бесы", "Белые ночи", "Записки из подполья", "Униженные и оскорблённые", "Двойник", "Сёмья Достоевского"]
- verdict: 

### e32 — «Хочу посмотреть фильм Uncharted»
- single:
  - search folder="Заметки" text="Uncharted фильм"
- staged:
  - append note="медиа" heading="смотреть" body=["Uncharted"]
- verdict: 

### e35 — «Хочу посмотреть фильм Uncharted и сериал Джентльмены сезон 2»
- single:
  - search folder="медиа" text="Uncharted фильм Джентльмены сериал сезон 2" scope="list"
- staged:
  - append note="медиа" heading="смотреть" body=["фильм Uncharted", "сериал Джентльмены сезон 2"]
- verdict: 

### e37 — «Хочу досмотреть пацаны сериал и Рик и морти»
- single:
  - note folder="Заметки" title="Медиа для просмотра" body=["Пацаны (сериал)", "Рик и морти"]
- staged:
  - append note="медиа" heading="смотреть" body=["пацаны сериал", "Рик и морти"]
- verdict: 

### e38 — «Посмотреть Джорджу бизара Adventures»
- single:
  - search folder="медиа" text="Джорджу бизара Adventures"
- staged:
  - append note="медиа" heading="смотреть" body=["Джорджу бизара Adventures"]
- verdict: 

### e43 — «Надо на массаж записаться»
- single:
  - update note="Задачи" task="Надо на массаж записаться" done=true
- staged:
  - task heading="моя херня" text="записаться на массаж" tags=["personal"]
- verdict: 

### e45 — «Я хочу сделать сам домой шведскую стенку ребёнку И создай страницу под мастерской для этого и найди планы как делаешь шведскую стенку стандартные Какие нужны бруски желательно Какого размера как и толщины такая там на Какой шаг ступенек и какие нужны палки для этого и крепления и всё вместе и несколько референсов можете съесть с планом сборки и размерами»
- single:
  - note folder="Области" title="Шведская стенка для ребёнка" props={"area": "gnezdo"} tags=["gnezdo"]
  - move note="Шведская стенка" to="Шведская стенка для ребёнка"
  - update note="Задачи" heading="gnezdo" task="Сделать шведскую стенку ребёнку #gnezdo"
  - append note="Шведская стенка для ребёнка" heading="Размеры и материалы" body=["Брусья — размер, толщина", "Ступеньки — шаг между ними", "Палки и крепления"]
  - append note="Шведская стенка для ребёнка" heading="Планы и референсы"
- staged:
  - note folder="Области" title="Шведская стенка для ребёнка" body=["Планы как делать шведскую стенку стандартные", "Какие нужны бруски желательно", "Какого размера как и толщины", "На какой шаг ступенек", "Какие нужны палки для этого", "Крепления", "Несколько референсов с планом сборки и размерами"] props={"tag": "gnezdo"} tags=["gnezdo"]
- verdict: 

### e46 — «Надо купить шампунь и бальзам для волос»
- single:
  - update note="Задачи" task="Купить шампунь #home" done=true
  - update note="Задачи" task="Купить бальзам для волос #home" done=true
- staged:
  - grocery body=["шампунь", "бальзам для волос"]
- verdict: 

### e49 — «Надо составить квиз на сегодняшнюю встречу книжного клуба»
- single:
  - update note="Задачи" task="Составить квиз на встречу книжного клуба #knub 📅 2026-09-21" done=true
- staged:
  - update note="Задачи" task="Составить квиз на встречу книжного клуба #knub 📅 2026-09-21"
- verdict: 

### e50 — «Задача с дедлайном сегодня составить квиз на встречу книжного клуба»
- single:
  - update note="Задачи" task="Составить квиз на встречу книжного клуба #knub 📅 2026-09-21" done=true
- staged:
  - update note="Задачи" task="Составить квиз на встречу книжного клуба #knub 📅 2026-09-21"
- verdict: 

### e53 — «посылка»
- single:
  - update note="Задачи" task="Забрать посылку из кауп #personal" done=true
- staged:
  - inbox text="посылка"
- verdict: 

### e54 — «Нужно рассмотреть возможность использовать Джефф в Боте»
- single:
  - search text="Джефф в Боте" tags=["personal"]
- staged:
  - update note="Задачи" task="Рассмотреть возможность использовать Джефф в Боте #personal"
- verdict: 

### e56 — «to do добавить приколюхи Санька»
- single:
  - update note="Задачи" task="Приколюхи Санька #personal" done=true
- staged:
  - task text="добавить приколюхи Санька"
- verdict: 

### e60 — «Глуховский регистрации»
- single:
  - update note="Задачи" task="встречу по следующей книге Глуховского регистрация" done=true
- staged:
  - inbox text="Глуховский регистрации"
- verdict: 

### e61 — «"регистрация"»
- single:
  - update note="Задачи" task="встречу по следующей книге Глуховского регистрация" done=true
- staged:
  - inbox text="\"регистрация\""
- verdict: 

### e65 — «идеи для торгового Бота чтобы пары появлялись в табах не фиксированные а те которые просканированы»
- single:
  - update note="Задачи" task="идеи для торгового Бота чтобы пары появлялись в табах не фиксированные а те которые просканированы"
- staged:
  - append note="пройекты" body=["идеи для торгового бота чтобы пары появлялись в табах не фиксированные а те которые просканированы"]
- verdict: 

### e69 — «задача добавить обсидиан Какие дубликации информации очень»
- single:
  - update note="Задачи" task="Добавить Obsidian. Какие дубликации информации?" done=true
- staged:
  - inbox text="задача добавить обсидиан Какие дубликации информации очень"
- verdict: 

### e70 — «Это задача»
- single:
  - task text="Это задача"
- staged:
  - inbox text="Это задача"
- verdict: 

### e71 — «задача заплатить за расходники в садик»
- single:
  - update note="Задачи" task="Заплатить за расходники в садик #home" done=true
- staged:
  - update note="Задачи" task="Заплатить за расходники в садик #home"
- verdict: 

### e72 — «кольцо 49/15.5»
- single:
  - update note="Задачи" task="кольцо 49/15.5" done=true
- staged:
  - inbox text="кольцо 49/15.5"
- verdict: 

### e74 — «купить Качельки»
- single:
  - update note="Задачи" task="Купить Качельки #personal" done=true
- staged:
  - update note="Задачи" task="Купить Качельки #personal"
- verdict: 

### e80 — «купить бритву»
- single:
  - update note="Задачи" task="купить бритву #personal" done=true
- staged:
  - update note="Задачи" task="купить бритву #personal"
- verdict: 

### e81 — «надо внести траты»
- single:
  - inbox text="надо внести траты"
- staged:
  - append note="бабосы" body=["траты"]
- verdict: 

### e83 — «купить яйца бекон мусорные пакеты тортик»
- single:
  - update note="Задачи" task="Купить яйца #home" done=true
  - update note="Задачи" task="Купить бекон #home" done=true
  - update note="Задачи" task="Купить мусорные пакеты #home" done=true
  - update note="Задачи" task="Купить тортик #personal" done=true
- staged:
  - grocery body=["яйца", "бекон", "мусорные пакеты"]
- verdict: 

### e84 — «личная задача для проекта nodogwalk - добавить стрик меню, пуп стрик, месячный календарь, настройки временных статов, сумма статов. сделай всё одной записью»
- single:
  - note folder="Заметки" title="nodogwalk" body=["Добавить стрик меню", "Пуп стрик", "Месячный календарь", "Настройки временных статов", "Сумма статов"] props={"area": "пройекты"} tags=["personal"]
- staged:
  - task heading="пройекты" text="добавить стрик меню, пуп стрик, месячный календарь, настройки временных статов, сумма статов в nodogwalk"
- verdict: 

### e85 — «рассмотреть идею проекта расширитель стола»
- single:
  - update note="Задачи" task="рассмотреть идею проекта расширитель стола #personal" done=true
- staged:
  - update note="Задачи" task="рассмотреть идею проекта расширитель стола #personal"
- verdict: 

### e86 — «прикрутить почту к саньку, после уточнения про jev»
- single:
  - update note="Задачи" task="прикрутить почту к саньку, после уточнения про jev #personal" done=true
- staged:
  - update note="Задачи" task="прикрутить почту к саньку, после уточнения про jev #personal"
- verdict: 

### e90 — «Найди мою страницу про шведскую стенку и там Найди те варианты которые не из фанеры А из дерева и не из металла А из дерева полностью и найди в интернете как сделать если тетива из бруса не из одного толстого А из двух например из двух досок по 2 см например и как их склеить и потом найди в местных магазинах например какие мне нужны материалы для этого проекта и где купить»
- single:
  - rewrite note="Шведская стенка" text="найти варианты из дерева полностью (не из фанеры и не из металла); найти как сделать тетиву из двух досок по 2 см вместо одного толстого бруса и как их склеить; найти в местных магазинах материалы для проекта и где купить"
- staged:
  - append note="Шведская стенка" body=["Варианты из дерева полностью, не из фанеры и не из металла", "Тетива из двух склеенных досок по 2 см", "Как склеить две доски для тетивы", "Материалы для проекта в местных магазинах", "Где купить материалы"]
- verdict: 

### e93 — «Найди мне страницу мою проворота тории и там с каждым подтипе Я хочу чтобы была картинка Как выглядит именно этот подтип То есть я хочу чтобы каждый вид и подвид ворот был у меня визуально тоже там виден»
- single:
  - search folder="Заметки" text="проворота тории виды подтипы"
- staged:
  - inbox text="Найди мне страницу мою проворота тории и там с каждым подтипе Я хочу чтобы была картинка Как выглядит именно этот подтип То есть я хочу чтобы каждый вид и подвид ворот был у меня визуально тоже там виден"
- verdict: 

### e94 — «заметка про ноут договор гвард на просмотр рекламы должен работать только когда уже когда нет какашек Потому что сейчас он сработал когда я сделал бесплатную уборку и после этого он мне не дал почистить рекламу»
- single:
  - append note="ноут договор гвард" body=["просмотр рекламы должен работать только когда нет какашек", "сейчас сработал когда я сделал бесплатную уборку и после этого не дал почистить рекламу"]
- staged:
  - rewrite note="ноут договор гвард" text="заметка про ноут договор гвард на просмотр рекламы должен работать только когда уже когда нет какашек Потому что сейчас он сработал когда я сделал бесплатную уборку и после этого он мне не дал почистить рекламу"
- verdict: 

### e96 — «я хочу чтобы ты создал новый раздел pehmevara и перенёс туда все гитхаб проекты»
- single:
  - note folder="Заметки" title="гитхаб проекты"
  - move note="pehmevara" to="гитхаб проекты" text="все гитхаб проекты"
- staged:
  - move note="пройекты" to="pehmevara" text="все гитхаб проекты"
- verdict: 

### e106 — «надо заказать новые очки»
- single:
  - update note="Задачи" task="заказать новые очки #personal" done=true
- staged:
  - update note="Задачи" task="заказать новые очки #personal"
- verdict: 

### e108 — «Thursday, Oct 1, 17:30 EEST просмотр квартиры Kalevipoja põik 3-120»
- single:
  - update note="Задачи" task="просмотр квартиры Kalevipoja põik 3-120 #personal 📅 2026-10-01" due="2026-10-01" scope="list" done=true
- staged:
  - update note="Задачи" task="просмотр квартиры Kalevipoja põik 3-120 #personal 📅 2026-10-01"
- verdict: 

### e109 — «фидбек по боту: рассылка 2 раза, продукты на главную, временной контекст теряется в плане, перемещение данных»
- single:
  - note folder="Заметки" title="Фидбек по боту" body=["рассылка 2 раза", "продукты на главную", "временной контекст теряется в плане", "перемещение данных"]
- staged:
  - inbox text="фидбек по боту: рассылка 2 раза, продукты на главную, временной контекст теряется в плане, перемещение данных"
- verdict: 
