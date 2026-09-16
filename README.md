# AntiGoblin

`AntiGoblin` — это панель управления для `Keenetic + Entware + XKeen/xray + sing-box`, которая живёт на самом роутере.

> [!IMPORTANT]
> **Если AntiGoblin уже установлен — удалять старую версию перед обновлением не нужно.**
> Запусти `update.sh`: он сделает backup пользовательского state и VPN-конфигов, обновит UI/backend/runtime и перезапустит сервисы. Чистое удаление нужно только если ты действительно хочешь отказаться от AntiGoblin.

### Самые нужные команды

После того как Entware уже работает и ты перешёл из Keenetic CLI в shell через `exec sh`:

```sh
# Первая установка
opkg install curl
/opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/install.sh | /opt/bin/sh

# Обновление уже установленной версии — state/ключи/правила сохраняются
/opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/update.sh | /opt/bin/sh

# Обычное удаление — перед удалением создаётся backup, VPN-конфиги сохраняются
/opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/uninstall.sh | /opt/bin/sh
```

UI после установки: `http://<IP-роутера>:8899/`. Логин и пароль — те же, что у web-интерфейса Keenetic.

> [!NOTE]
> Команды выше скачивают файлы из `Lagbag/AntiGoblin:main`. Они начнут ставить **именно эту обновлённую сборку после того, как ты зальёшь её в свой fork**. Если используешь ZIP, скачанный из этого чата, ничего пушить не обязательно: распакуй его, скопируй папку на Entware-накопитель роутера и запусти локальную установку/обновление из раздела ниже.

Для ZIP/локальной копии теперь достаточно запустить `update.sh` прямо из каталога релиза — он **сам возьмёт соседний `install.sh`**, а не полезет за старым `main` на GitHub:

```sh
cd /путь/к/AntiGoblin-main
/opt/bin/sh ./update.sh
```

Чтобы намеренно обновиться именно из GitHub, можно использовать обычную `curl .../update.sh | sh` команду выше или запустить локальный скрипт с `ANTIGOBLIN_UPDATE_REMOTE=1`.

После установки рабочий сценарий пользователя:

1. Открыть UI на `http://<router-ip>:8899/`.
2. Добавить **ключ** — вручную через поддерживаемый proxy URI (`vless://`, `vmess://`, `hysteria2://`, `trojan://`, `ss://`, `tuic://` и др.), вставить standalone sing-box/Hiddify JSON или подключить **подписку** по HTTPS-URL.
3. Выбрать активный ключ (radio-кнопка в блоке «Активный ключ»).
4. Создать routing-группы.
5. Нажать `Сохранить и применить`.
6. В Keenetic web UI назначить нужные устройства в политику `xkeen` в разделе «Приоритеты подключений».

После этого роутер использует:

- политику Keenetic `xkeen` для выбора устройств;
- `iptables` для перехвата `TCP` и `UDP` устройств из `xkeen`;
- `xray` для transparent TCP на `:61219`: VLESS/VMess он терминирует сам, остальные активные протоколы передаёт в локальный SOCKS5-мост sing-box `127.0.0.1:61225`;
- `sing-box` для UDP TPROXY на `:61221` и для upstream-протоколов, которые не терминируются xray напрямую.

Текущая живая модель runtime:

- любая UI-группа с outbound `vless-reality` гонит и `TCP`, и `UDP` через VPN — отдельных флагов для UDP нет;
- группы с outbound `bypass` обходят `xray` полностью через `RETURN`;
- группы с outbound `direct` входят в `xray`, но уходят напрямую без VPN;
- общий `UDP` устройств вне VPN-групп идёт напрямую;
- локалка и discovery обходят `xray` через `RETURN`;
- дополнительно на LAN-IP роутера поднят **SOCKS5-inbound (порт 61080, TCP+UDP)** — для точечного per-process туннеля с PC ([подробнее](#точечный-туннель-для-приложений-с-pc-socks5-inbound)).

### Автовыбор сервера по задержке

Автовыбор включён по умолчанию. Раз в **5 минут** AntiGoblin проверяет все серверы активного профиля параллельно и сортирует их по измеренной задержке. Для TCP-протоколов измеряется время TCP connect к реальному порту сервера; для QUIC/UDP-протоколов (`Hysteria/Hysteria2`, `TUIC`, `WireGuard`) используется ICMP RTT, потому что универсального безопасного handshake для всех этих протоколов нет.

После измерения AntiGoblin пытается включить самый быстрый сервер и дополнительно делает **реальный HTTPS-запрос через локальный SOCKS5 → активный туннель**. Если самый быстрый endpoint отвечает на ping, но сам VPN на нём не работает/не принимает ключ, watchdog откатывает конфиг и пробует следующий сервер по RTT. По умолчанию порог переключения — `0 ms`, то есть выбирается минимальная актуальная задержка; в UI можно выставить положительный порог, если хочется меньше переключений из-за джиттера.

После каждого `Сохранить и применить` полный latency-check запускается сразу, не нужно ждать следующего пятиминутного цикла. Результаты видны рядом с каждым сервером и в логе `auto-select`.

## Если `routing.json` выглядит правильно, но сайты не идут через VPN

Например, такой финальный rule:

```json
{
  "type": "field",
  "inboundTag": ["redirect"],
  "outboundTag": "vless-reality"
}
```

означает: **весь TCP, который уже попал в Xray inbound `redirect`, должен уйти через активный VPN**. Если при таком конфиге `2ip.io` всё равно показывает обычный WAN-IP, проблема почти наверняка не в `routing.json`, а до него — трафик не попал в `redirect:61219`.

В этой версии `Сохранить и применить` больше не считает работу успешной только потому, что JSON записался. Backend одной транзакцией валидирует будущие Xray/sing-box конфиги, перезапускает ядра и до ответа UI проверяет/восстанавливает `iptables nat PREROUTING → xkeen → REDIRECT :61219`. Тяжёлый DNS/ipset refresh затем запускается self-heal отдельно.

Проверка на роутере:

```sh
# Есть ли TCP-перехват и растёт ли счётчик пакетов
iptables -t nat -L PREROUTING -nv --line-numbers | grep xkeen
iptables -t nat -L xkeen -nv --line-numbers

# Сам туннель без Keenetic policy: запрос напрямую через SOCKS AntiGoblin
/opt/bin/curl -fsS --socks5-hostname 127.0.0.1:61080 https://api.ipify.org

# Логи
tail -n 100 /opt/var/log/xray-manual.log
tail -n 100 /opt/var/log/sing-box-xkeen.log
tail -n 100 /opt/var/log/xkeen-selfheal.log
```

Если SOCKS-команда показывает VPN-IP, но счётчик `xkeen` остаётся `0`, назначь тестовое устройство политике Keenetic **`xkeen`** в `Приоритеты подключений`. UI теперь также показывает это различие явно: «хук отсутствует» и «хук есть, но 0 пакетов» — разные состояния.

## Поддерживаемые протоколы и транспорты

В одном профиле можно держать **несколько ключей** (manual или из подписки) и переключаться между ними одной кнопкой. Все парсятся универсально:

| Протокол | Транспорты | Безопасность | Тоннелирует через |
|----------|-----------|--------------|-------------------|
| **VLESS** | `tcp`, `ws`, `grpc`, `xhttp` | `reality`, `tls`, `none` | xray для URI; raw sing-box JSON остаётся на sing-box |
| **VMess** | `tcp`, `ws`, `grpc` | `tls`, `reality`, `none` | xray для URI; raw sing-box JSON остаётся на sing-box |
| **Hysteria2 / HY2** | `quic` | `tls`, Salamander obfs, cert pin | sing-box |
| **Hysteria v1** | `quic` | `tls`, obfs, up/down Mbps | sing-box |
| **Trojan** | `tcp`, `ws`, `grpc`, `httpupgrade`, `http/h2` | `tls` | sing-box |
| **Shadowsocks** | TCP/UDP | SIP002 / legacy `ss://`, plugin/options | sing-box |
| **TUIC** | `quic` | `tls`, congestion control, UDP relay, 0-RTT | sing-box |
| **AnyTLS** | TCP | `tls` | sing-box |
| **SOCKS4/4a/5** | TCP/UDP по возможностям upstream | optional auth | sing-box |
| **HTTP / HTTPS CONNECT** | TCP | optional auth, TLS для HTTPS | sing-box |
| **SSH** | TCP | password; private-key поля сохраняются в state | sing-box |
| **Naive** | HTTPS / QUIC | `tls` | Hiddify sing-box |
| **WireGuard** | L3 endpoint | raw Hiddify/sing-box `endpoints[]` | sing-box endpoint (1.13+) |

Особенности:

- **XHTTP** разбирает все `mode`-варианты (`auto`/`packet-up`/`stream-up`/`stream-one`) и весь `extra={...}` блок (`scMaxEachPostBytes`, `scMaxConcurrentPosts`, `scMinPostsIntervalMs`, `xPaddingBytes`, `noGRPCHeader` и т. п.). Это критично — без правильно прокинутого `extra` стрим-up handshake не складывается и сервер скатывается в Reality fallback HTML.
- **gRPC** покрывает `serviceName`, `mode` (`multi`/`gun`), `authority` и `alpn`.
- **Все sing-box-backed протоколы** работают через одну bridge-схему: xray-outbound с историческим тегом `vless-reality` становится SOCKS5-hop в локальный mixed-inbound sing-box (`127.0.0.1:61225`), а sing-box держит реальный upstream. UDP-TPROXY на `61221` уходит в тот же активный outbound напрямую.
- **Hiddify/sing-box JSON**: подписка или ручной импорт может содержать полный `outbounds[]`, `endpoints[]`, один standalone outbound или endpoint. Сам узел сохраняется почти без преобразования, поэтому provider-specific поля (например XHTTP extensions и расширенные WireGuard options) не теряются. Selector/urltest/direct и цепочки с `detour` не импортируются как один ключ — им нужны зависимые узлы.
- Installer на `arm64/aarch64` и `amd64/x86_64` сначала ставит `hiddify-sing-box`; если подходящего Hiddify asset нет, откатывается на официальный sing-box. Это даёт расширения Hiddify там, где бинарь их реально поддерживает.

### Подписки

Подписка — это HTTPS-URL, который отдаёт base64-кодированный или plain-text список URI (по одному ключу на строку), либо sing-box/Hiddify JSON с `outbounds[]`/`endpoints[]`. Для URI поддерживается стандартный формат `subconverter`/`v2sub`. AntiGoblin:

- по HTTPS-only, с лимитом ответа 256KB и таймаутом 10 секунд;
- хранит URL подписки в `xkeen-ui-state.json` (root-only на роутере);
- обновляется по кнопке ↻ в UI (auto-refresh пока не реализован);
- при refresh добавляет новые ключи, помечает удалённые, оставляет неизменные на месте; активный ключ сохраняется, если он всё ещё в подписке.

### Точечный туннель для приложений с PC (SOCKS5-inbound)

Помимо device-based политики (весь трафик PC через VPN), на LAN-IP роутера поднят **SOCKS5-inbound** на порту `61080` (TCP+UDP). Он позволяет из Windows заворачивать через VPN **только выбранные приложения по имени процесса** — через любой Windows-клиент с process-based SOCKS5-перехватом. Полезно когда IP серверов приложения непредсказуемые (динамические / anycast) и добавлять их в CIDR-группы вручную неудобно.

Настройка на клиенте:

- SOCKS5-сервер: LAN-IP роутера, порт `61080`, без auth.
- Handle-list: имена целевых `.exe` (для приложений с дочерними процессами полезно включить опцию «handle child processes»).
- LAN-трафик клиент **не должен** захватывать (иначе получится петля на сам SOCKS-сервер).

Трафик по цепочке: `App → SOCKS5-клиент → LAN-IP:61080 → xray socks-in → routing (socks-in → vless-reality) → активный ключ VPN`. TCP и UDP приложения идут одним путём с одного exit-IP — исключает split-horizon-разрывы.

LAN-IP роутера подставляется в inbound-конфиг **динамически** — функция `xkeen_ensure_socks_inbound_ip` в `xkeen-runtime.sh` при каждом apply/restart определяет адрес через интерфейс `br0`. Схема работает на любом LAN-IP без правки конфига.

## Оглавление

- [Поддерживаемые протоколы и транспорты](#поддерживаемые-протоколы-и-транспорты)
  - [Подписки](#подписки)
  - [Точечный туннель для приложений с PC (SOCKS5-inbound)](#точечный-туннель-для-приложений-с-pc-socks5-inbound)
- [Подготовка Keenetic (один раз руками)](#подготовка-keenetic-один-раз-руками)
  - [Совместимые модели](#совместимые-модели)
  - [Шаг 1. Установить компоненты KeeneticOS](#шаг-1-установить-компоненты-keeneticos)
  - [Шаг 2. Подготовить флешку с Entware на PC](#шаг-2-подготовить-флешку-с-entware-на-pc)
  - [Шаг 3. Подключить Entware к OPKG-менеджеру и перезагрузить](#шаг-3-подключить-entware-к-opkg-менеджеру-и-перезагрузить)
- [Установка](#установка)
  - [Вариант 1 — «всё-на-флешке» (рекомендуемый, без SSH и без web CLI)](#вариант-1--всё-на-флешке-рекомендуемый-без-ssh-и-без-web-cli)
  - [Вариант 2 — через Keenetic Web CLI](#вариант-2--через-keenetic-web-cli)
  - [Вариант 3 — через SSH](#вариант-3--через-ssh)
  - [Как открыть UI после установки](#как-открыть-ui-после-установки)
- [Что делать после установки](#что-делать-после-установки)
- [Где брать списки IP / CIDR / доменов для популярных сервисов](#где-брать-списки-ip--cidr--доменов-для-популярных-сервисов)
- [Структура проекта](#структура-проекта)
- [Источник истины](#источник-истины)
- [Runtime-файлы на роутере](#runtime-файлы-на-роутере)
- [Обновление без удаления](#обновление-без-удаления)
- [Удаление](#удаление)
- [Разработка](#разработка)
- [Инварианты проекта](#инварианты-проекта)
- [Правила проекта](#правила-проекта)
- [Что под капотом и кому спасибо](#что-под-капотом-и-кому-спасибо)
- [Лицензия и пользовательское соглашение](#лицензия-и-пользовательское-соглашение)

## Подготовка Keenetic (один раз руками)

### Совместимые модели

Подходит любой Keenetic c USB-портом и поддержкой Entware. Live-инсталляция, на которой проект разрабатывался и проверялся — **Netcraze Giga** (это Keenetic Giga KN-1010 под пост-2024 брендом РФ-рынка, ARM-сборка KeeneticOS). На других ARM-роутерах Keenetic должно работать без изменений. MIPS-модели (Lite/4G/Air) формально совместимы, но `xray + sing-box` под MIPS ставить тяжелее и performance скромнее.

Минимальные требования:

- USB-порт (USB 2.0 хватает, USB 3.0 быстрее)
- KeeneticOS 3.5+ (показывается в web UI в разделе «Системный монитор»)
- USB-флешка 4 ГБ+ (Entware занимает ~150 МБ, остальное под логи и кэш opkg)

Архитектуру процессора можно посмотреть в web UI Keenetic в `Системный монитор → Системная информация` или проверить через SSH командой `uname -m`: `aarch64` / `armv7l` / `mipsel` / `mips`.

### Шаг 1. Установить компоненты KeeneticOS

В web UI Keenetic зайти в `Управление → Общие настройки → Изменить набор компонентов` и добавить:

- **«Поддержка открытых пакетов»** (раздел «Пакеты OPKG») — открывает менеджер OPKG в web UI.
- **«Файловая система Ext4»** (раздел «Файловые системы») — Entware ставится только на ext-разделы.
- **«Модули ядра подсистемы Netfilter»** (раздел «Сетевые функции») — нужно для `iptables`.
- **«Пакет расширения Xtables-addons для Netfilter»** — нужно для `TPROXY`, `xt_set`, `connmark`.
- **«Доступ через SSH»** (раздел «Сетевые функции») — не нужен для установки через Web CLI, но полезен как запасной способ диагностики.

Желательно дополнительно:

- **«Модули ядра подсистемы Traffic Control»** — некоторые netfilter-модули тянут её зависимостью.

Применить, дождаться перепрошивки и автоматического reboot.

### Шаг 2. Подготовить флешку с Entware на PC

Флешку готовим целиком на компьютере: форматируем в EXT4, кладём правильный installer-tarball, и только потом втыкаем в роутер. Web UI Keenetic сам Entware из интернета не качает — он распакует уже подготовленный installer на первом reboot.

**1. Узнать архитектуру роутера.**

Архитектуру можно посмотреть в web UI Keenetic в `Системный монитор → Системная информация` (показывает процессор) или ориентироваться по модели:

| Модель / процессор | Папка на bin.entware.net | Имя installer-tarball |
|--------------------|--------------------------|------------------------|
| Netcraze Giga, KN ARM64 (Hopper, Peak, Skipper, Hero, Speedster, Ultra KN-1811) | `aarch64-k3.10` | `aarch64-installer.tar.gz` |
| Старшие ARMv7 | `armv7sf-k3.2` | `armv7sf-installer.tar.gz` |
| Lite / 4G / Air (LE-MIPS) | `mipsel-k3.4` | `mipsel-installer.tar.gz` |
| Старые BE-MIPS | `mips-k3.4` | `mips-installer.tar.gz` |

**2. Скачать installer-tarball на PC.**

Собрать URL вида `https://bin.entware.net/<папка>/installer/<имя>.tar.gz`. Для Netcraze Giga это `https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz`. Сохранить файл к себе на PC.

**3. Отформатировать флешку в EXT4.**

Windows нативно ext-разделы не создаёт, нужен сторонний инструмент:

- **MiniTool Partition Wizard Free** — самый дружелюбный к новичкам (выбрать диск → Format → File system: Ext4 → Apply).
- **DiskGenius Free** — аналог.
- **WSL** или Linux: `sudo mkfs.ext4 /dev/sdX1`.

Файловая система — `EXT4`, метку (label) задать осмысленную, например `OPT`.

**4. Скопировать installer на флешку.**

В корне отформатированной флешки создать каталог `install/` и положить туда скачанный `<arch>-installer.tar.gz` **как есть**, без распаковки. Структура должна быть такая:

```text
<USB root>/
└── install/
    └── aarch64-installer.tar.gz   (пример для Netcraze Giga)
```

> **Почему именно `install/`.** Это Keenetic-специфика. Когда ты в web UI сохраняешь настройки `Менеджера пакетов OPKG`, демон `npkg` внутри KeeneticOS монтирует флешку и ищет в корне раздела папку `install/` с архивом `<arch>-installer.tar.gz`. Если находит — сам распаковывает его в `/opt` и поднимает окружение Entware. В системном логе при этом видно строку вида `npkg: inflating aarch64-installer.tar.gz`. Если положить tarball в любую другую папку или сразу распаковать — `npkg` его не подхватит.

Безопасно извлечь флешку из PC и воткнуть в USB-порт роутера.

### Шаг 3. Подключить Entware к OPKG-менеджеру

В web UI: `Приложения → Менеджер пакетов OPKG`.

1. В поле **«Накопитель»** выбрать только что вставленную флешку (она показывается с EXT4-меткой и UUID).
2. Поле **«Сценарий initrc»** оставить как есть: `/opt/etc/init.d/rc.unslung`.
3. В таблице **«Пользователь»** дать галку доступа `admin` (и/или отдельно созданному `root`).
4. Сохранить настройки и дождаться завершения установки.

Keenetic автоматически распакует tarball из `install/` в файловую систему `/opt` на флешке и подготовит окружение Entware в текущем сеансе — перезагрузка не нужна.

После завершения можно проверить Entware через SSH:

```sh
ssh admin@192.168.1.1
exec sh                    # выйти из NDM CLI в обычный shell
/opt/bin/opkg --version
```

Если `opkg` отвечает версией — подготовка закончена, можно ставить AntiGoblin. Для установки через Web CLI этот проверочный шаг можно пропустить: достаточно дождаться завершения и перейти к следующему разделу.

> На некоторых прошивках Keenetic (с включённым SSH-компонентом) порт SSH — `222`, а пользователь по умолчанию — `root` с паролем `keenetic`. Если `ssh admin@192.168.1.1` не пускает, попробуй `ssh root@192.168.1.1 -p 222`.

## Установка

Если это **обновление существующей установки**, сразу переходи в раздел [Обновление без удаления](#обновление-без-удаления). Повторная установка поверх старой версии тоже безопасна, но `update.sh` понятнее и явно проверяет, что старая установка существует.

Три варианта для **первой установки**, от самого простого к самому «hands-on». Все три ведут к одному результату — рабочему UI на роутере. Флеш-путь рекомендуется тем, кто не хочет открывать терминал.

### Вариант 1 — «всё-на-флешке» (рекомендуемый, без SSH и без web CLI)

Ничего не вводишь руками. Скачал zip → распаковал на флешку → воткнул → активировал OPKG в web UI. Всё.

Как это работает: zip содержит папку `install/` с уже готовым `<arch>-installer.tar.gz`, внутри которого лежит Entware **плюс** предустановленный AntiGoblin и `sing-box`. Keenetic развернёт Entware, при первом старте одноразовый `S99antigoblin-firstboot.sh` запустит `install.sh` из локальной копии без похода в GitHub — репозиторий уже внутри архива.

**Шаги:**

1. Скачать `antigoblin-usb-<arch>.zip` из [GitHub Releases](https://github.com/Lagbag/AntiGoblin/releases):

   - `antigoblin-usb-aarch64.zip` — большинство современных Keenetic: Giga, Ultra, Hero, Peak, Speedster, Runner, Hopper, Skipper.
   - `antigoblin-usb-armv7.zip` — старые: Extra, Giga II/III, Duo, Air, Omni.

   > Внутри zip лежит папка `install/` с правильно названным `<arch>-installer.tar.gz` — папку и файл создавать вручную не нужно. Внутри также лежит файл `VERSION`, который позже можно прочитать на роутере (см. ниже).

2. Флешку подготовить как обычно ([Шаг 2](#шаг-2-подготовить-флешку-с-entware-на-pc)) — отформатировать в ext4.

3. **Распаковать zip прямо в корень флешки.** На Windows — правой кнопкой по zip → «Извлечь всё…» → указать буквы флешки (например `E:\`). На Linux/macOS — `unzip antigoblin-usb-aarch64.zip -d /mnt/usb/`. Должна получиться такая структура:

   ```text
   <буква флешки>/
     └── install/
         └── aarch64-installer.tar.gz
   ```

4. Вставить флешку в роутер, в web UI Keenetic зайти в `Приложения → Менеджер пакетов OPKG` и указать эту флешку — [Шаг 3](#шаг-3-подключить-entware-к-opkg-менеджеру). Entware развернётся в текущем сеансе.

5. Подождать ещё ~1-2 минуты: `S99antigoblin-firstboot.sh` сам дождётся готового Entware и запустит установку в фоне. Прогресс можно смотреть, открывая UI в браузере (страница появится, как только сервис поднимется — адрес см. ниже [Как открыть UI после установки](#как-открыть-ui-после-установки)). Если нужен подробный лог — SSH и `cat /opt/var/log/antigoblin-firstboot.log`.

6. Открыть UI, залогиниться через Keenetic-креды, добавить ключ / подписку, Save & Apply. Дальше как в разделе [Что делать после установки](#что-делать-после-установки).

> Если предпочитаешь класть tarball руками — в том же Release лежит `<arch>-installer.tar.gz` рядом с zip'ом. Скачал → сам создал `install/` на флешке → положил.

> Проверить версию установленной сборки на роутере: `ssh admin@<адрес-роутера>` → `exec sh` → `cat /opt/share/antigoblin-staged/VERSION`.

**Как это устроено под капотом.** Наш USB-tarball — это исходный `<arch>-installer.tar.gz` от Entware плюс:

- `/opt/sbin/sing-box` (уже нужного arch);
- `/opt/share/antigoblin-staged/` — полная копия репозитория;
- `/opt/etc/init.d/S99antigoblin-firstboot.sh` — one-shot init-скрипт, который ждёт готовности NDM, запускает `install.sh` с флагом `ANTIGOBLIN_SRC_DIR=/opt/share/antigoblin-staged` (без похода в GitHub), после успеха `touch /opt/etc/antigoblin.done` и `rm` себя из init.d — второй раз при следующем boot не сработает.

**Собрать свой USB-installer** (например, если хочешь пропатчить репозиторий перед раскаткой):

```bash
./scripts/xkeen/build-usb-installer.sh --arch aarch64 --version my-build
# → dist/antigoblin-usb-aarch64.zip   (рекомендуется — юзер распаковывает в корень флешки)
# → dist/aarch64-installer.tar.gz     (сырой tarball, кладётся руками в install/)
# Версия зашивается внутрь как /opt/share/antigoblin-staged/VERSION.
```

Билд-скрипт запускается на Linux или WSL (Ubuntu/Debian хватает), требует `bash`, `curl`, `tar`, `gzip`, `awk`. Ничего не кросс-компилирует — только скачивает готовые бинарники и репаковывает. На голом Windows Git-Bash **не работает** — не умеет создавать symlink'и, которых полно в Entware installer; используй WSL. Автоматическая сборка на GitHub Actions описана в `.github/workflows/release-usb-installer.yml` — при пуше тега `v*` собираются aarch64 и armv7, публикуются в Release.

**Ограничения:**

- Поддерживаемые архитектуры — `aarch64` и `armv7`. Для `mipsel`/`mips`/`x86_64` используй Вариант 2 или 3.
- `S99antigoblin-firstboot.sh` при запуске выполняет `opkg install` для обязательных пакетов (`xray`, `uhttpd_kn`, `iptables`, `ipset`, `conntrack`, `jq`, `gawk`, `ca-bundle`). Для этого нужен работающий WAN. Если WAN недоступен на первом boot — flash-install зафейлится, лог в `/opt/var/log/antigoblin-firstboot.log`.
- Если хочется прогнать firstboot ещё раз (например, после сброса) — удалить `/opt/etc/antigoblin.done` и перезагрузить роутер.

### Вариант 2 — через Keenetic Web CLI

Для случая, когда Entware уже развёрнут ([Шаги 1-3 подготовки Keenetic](#подготовка-keenetic-один-раз-руками)), но качать zip и распаковывать не хочется. Достаточно браузера — SSH-клиент не нужен.

Открыть встроенный Web CLI Keenetic:

```text
http://<адрес-роутера>/a
```

(например `http://my.keenetic.net/a` или `http://192.168.1.1/a` — см. [ниже](#как-открыть-ui-после-установки) как узнать адрес роутера).

Ввести команду:

```sh
exec sh -c "opkg install curl >/dev/null 2>&1; /opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/scripts/xkeen/antigoblin-web-cli-install.sh | /opt/bin/sh"
```

Что происходит:

- Keenetic Web CLI запускает shell-команду через `exec sh -c`;
- `opkg install curl` тихо доставит `curl`, если его ещё нет (базовый Entware ставит только `wget-nossl`, без HTTPS — своим `wget` этот bootstrap не скачать);
- Entware-овский `/opt/bin/curl` скачивает `scripts/xkeen/antigoblin-web-cli-install.sh`;
- этот bootstrap-скрипт скачивает обычный `install.sh` из репозитория и запускает его через `/opt/bin/sh`.

### Вариант 3 — через SSH

Классический путь для тех, кто уже привык к терминалу. Требует SSH-доступа к роутеру (компонент «Доступ через SSH» в KeeneticOS).

```sh
ssh admin@<адрес-роутера>
exec sh
opkg install curl
/opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/install.sh | /opt/bin/sh
```

Подставь адрес роутера ([как узнать](#как-открыть-ui-после-установки)) вместо `<адрес-роутера>` — например `my.keenetic.net` или `192.168.1.1`.

> **`exec sh` обязателен**. После `ssh admin@…` Keenetic пускает в свой NDM CLI (промпт `(config)>`), а не в обычный shell. Если ввести `curl …` сразу — увидишь `unknown command`. `exec sh` переключает сессию в BusyBox-shell, где работают `curl`, `opkg`, `install.sh` и всё остальное.

> На дефолтном Entware стоит `wget-nossl` (без HTTPS), поэтому `wget https://...` не сработает. Если `curl` ещё не установлен — сначала `opkg install curl`, потом команду выше.

Если `curl` ещё не установлен, а хочется сначала посмотреть скрипт:

```sh
ssh admin@<адрес-роутера>
exec sh
opkg install curl
/opt/bin/curl -fsSL -o /opt/tmp/antigoblin-install.sh https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/install.sh
/opt/bin/sh /opt/tmp/antigoblin-install.sh
```

Скрипт (общий для всех трёх вариантов):

- проверяет `Entware/OPKG` в `/opt`;
- ставит пакеты Entware (`xray`, `uhttpd_kn`, `jq`, `iptables`, `ipset`, `conntrack`, `ca-bundle`, `wget`, `tar`, `gzip` и др.);
- на `arm64/aarch64` и `amd64/x86_64` сначала скачивает `hiddify-sing-box` (для Hiddify-расширений), а при недоступности/неподдерживаемой архитектуре использует официальный `sing-box`; бинарь кладётся в `/opt/sbin/sing-box`;
- скачивает свежий tarball репозитория с GitHub и распаковывает во временный каталог;
- создает UI-видимую политику Keenetic `xkeen` как `Policy42+`, если ее еще нет;
- раскладывает sample-конфиги `xray` и `sing-box` (существующие конфиги не трогаются — для перезаписи использовать `ANTIGOBLIN_FORCE=1`);
- кладет UI и backend в `/opt/share/xkeen-manager/`;
- ставит init-скрипты `S20antigoblin-sysctl`, `S24antigoblin-singbox`, `S25antigoblin-selfheal`, `S26antigoblin`;
- ставит cron и `ndm/usb.d`/`ndm/netfilter.d` хуки для авто-восстановления после reboot, USB-событий и reload netfilter;
- запускает первый цикл `xkeen-selfheal.sh --force` и поднимает UI на `:8899`.

Скрипт идемпотентный: повторный запуск обновит исходники без затирания пользовательской конфигурации. При обнаружении старой установки он перед заменой файлов создаёт backup в `/opt/var/backups/antigoblin/pre-upgrade-<дата>/`.

Доступные режимы локального `install.sh`:

```sh
sh install.sh            # auto: первая установка или upgrade-in-place
sh install.sh --update   # обновить; ошибка, если старой установки нет
sh install.sh --install  # установка; безопасно и поверх старой версии
sh install.sh --force    # плюс пересев безопасных sample-конфигов
```

`--force` **не нужен для обычного обновления**. Пользовательские `04_outbounds.json`, `05_routing.json` и state сохраняются.

### Как открыть UI после установки

Открыть в браузере: `http://<адрес-роутера>:8899/`. Логин и пароль — от web UI Keenetic.

**Какой адрес подставить:**

- `http://my.keenetic.net:8899/` — универсально работает, если клиент подключён к Keenetic и включён компонент «Служба Keenetic DNS» (стоит по умолчанию). Не зависит от того, менялся ли LAN-IP роутера.
- `http://192.168.1.1:8899/` — заводской дефолт LAN-IP Keenetic. Работает, если ты его не менял.
- **Свой IP роутера**, если ты менял LAN-подсеть. Пример: у нас в тестовой инсталляции роутер стоит на `192.168.2.1`, поэтому UI живёт на `http://192.168.2.1:8899/`.

**Как узнать текущий адрес роутера:**

- В web UI Keenetic: `Мои сети и Wi-Fi → Домашняя сеть → Адрес роутера`.
- Или через command line на клиенте:
  - Windows: `ipconfig` — строка «Основной шлюз» под активным интерфейсом.
  - Linux / macOS: `ip route | grep default` или `netstat -rn | grep default`.
  - Android: настройки Wi-Fi → текущая сеть → «Шлюз».
- Или посмотреть последнюю строчку вывода `install.sh` — он сам пишет URL для UI по завершении.

Порт `:8899` — дефолт AntiGoblin. Меняется через `-Port` в PowerShell-скриптах разработки или редактированием `/opt/etc/antigoblin.conf` на роутере.

## Что делать после установки

В UI:

1. **Добавить ключ или подписку** в блоке «Конфиг прокси»:
   - **+ Ручной ключ** — вставить поддерживаемый URI (`vless://`, `vmess://`, `hysteria2://`, `hysteria://`, `trojan://`, `ss://`, `tuic://`, `anytls://`, `socks5://`, `http(s)://`, `ssh://`, `naive+https://` / `naive+quic://`) или standalone sing-box/Hiddify JSON. Парсер сам разложит URI на поля; raw JSON сохранит upstream outbound/endpoint (включая современный WireGuard `endpoints[]`).
   - **+ Подписка** — добавить HTTPS-URL подписки. Backend стянет, распарсит, добавит каждый ключ как отдельную карточку.
2. **Выбрать активный ключ** — radio-кнопка в «Активный ключ». Через него пойдёт весь VPN-трафик.
3. **Создать или включить routing-группы.** У каждой группы выбрать outbound:
   - `vless-reality` — TCP и UDP этой группы идут через активный ключ (тег `vless-reality` остаётся одинаковым независимо от протокола ключа — это просто маршрут «через VPN»);
   - `direct` — трафик группы входит в `xray` и выходит без VPN;
   - `bypass` — трафик группы обходит `xray` полностью (через `RETURN`).
4. **Нажать `Сохранить и применить`.** Backend пересоберёт `04_outbounds.json` (xray) и при необходимости `sing-box-xkeen.json`, перезапустит оба процесса.

В web UI Keenetic в разделе «Приоритеты подключений» — назначить нужные устройства в политику `xkeen`. Только устройства из этой политики попадают под управление AntiGoblin; остальные политики (например, личная `no_vpn`) не трогаются.

## Где брать списки IP / CIDR / доменов для популярных сервисов

Для routing-групп удобно набивать не только домены, но и CIDR-диапазоны — особенно для сервисов с UDP/RTC (Discord voice, FaceTime, Zoom), у которых пакеты часто идут на голые IP без TLS-SNI. Полезные источники:

- **[iplist.opencck.org](https://iplist.opencck.org/)** — open-source агрегатор. По одному эндпоинту отдает актуальные списки IP/CIDR/доменов для Discord, Telegram, ChatGPT, Cloudflare, Twitter, Meta, Google, YouTube, Apple, Spotify и других популярных сервисов. Формат: текст, JSON, CIDR. Удобно копировать прямо в UI-группу. Сам сервис — open-source проект [rekryt/iplist](https://github.com/rekryt/iplist) под MIT-лицензией; если он вам помог, поставьте звезду и автору.
- **[bgp.he.net](https://bgp.he.net/)** — поиск по AS-номеру или организации, выдает все BGP-префиксы. Полезно, когда нужно найти все CIDR конкретного провайдера/CDN (например, Cloudflare AS13335, Discord AS49544).
- **[ipinfo.io](https://ipinfo.io/)** — лукап одного IP. Покажет ASN, организацию, страну.
- **Официальные списки CDN:**
  - Cloudflare: https://www.cloudflare.com/ips/
  - Apple iCloud Private Relay: https://mask-api.icloud.com/egress-ip-ranges.csv
  - GitHub: https://api.github.com/meta
- **[dnscheck.tools](https://dnscheck.tools/)** — посмотреть, на какие IP резолвится домен у разных DNS-резолверов. Помогает при отладке «работает у меня, не работает на роутере».

Практическое правило: если сервис только TCP (сайт, API) — обычно хватает домена. Если есть UDP/QUIC/RTC/voice — добавляй и CIDR из `iplist.opencck.org`, иначе routing-rule по домену не успеет сработать (у пакета нет SNI).

## Структура проекта

- [docs/architecture.md](docs/architecture.md) — текущая архитектура на Keenetic, Entware, xray и sing-box.
- [docs/project-map.md](docs/project-map.md) — где лежит код, скрипты, конфиги и документация.
- [docs/troubleshooting.md](docs/troubleshooting.md) — типовые проблемы и как их решить.
- [docs/xkeen-manager-ui.md](docs/xkeen-manager-ui.md) — как пользоваться UI и как выкатывать проект.

## Источник истины

Единственный источник истины для UI:

- `/opt/share/xkeen-manager/xkeen-ui-state.json` — профили, список подписок, ключей, активный ключ, mux-настройки, routing-группы.

Из него UI/backend генерируют (и каждый Save+Apply перезаписывает):

- `/opt/etc/xray/configs/04_outbounds.json` — outbound активного ключа. Для sing-box-backed протоколов это SOCKS5 в локальный sing-box.
- `/opt/etc/xray/configs/05_routing.json` — правила маршрутизации UI-групп.
- `/opt/etc/sing-box/xkeen.json` — для Xray-native VLESS/VMess это TPROXY+SS-relay; для остальных активных протоколов это TPROXY+mixed-inbound+реальный upstream outbound.

Перед каждой записью бэкенд сохраняет копию с суффиксом `.bak-ui-<timestamp>` — откатить руками можно `cp`-ом.

## Runtime-файлы на роутере

UI и backend:

- `/opt/share/xkeen-manager/index.html`, `app.js`, `styles.css`
- `/opt/share/xkeen-manager/api/routing.cgi`
- `/opt/share/xkeen-manager/api/xkeen-selfheal.sh`
- `/opt/share/xkeen-manager/api/xkeen-runtime.sh`

Bypass собирается только из UI-групп с outbound `bypass`. Их домены и CIDR попадают в runtime `xkeen_bypass` и обходят `xray` через `RETURN`.

UDP-маршрутизация привязана к outbound группы автоматически: для любой включенной группы с outbound `vless-reality` её домены/CIDR попадают в `xkeen_udp_route`, и совпавший UDP уходит в `TPROXY → sing-box:61221`. Для VLESS/VMess sing-box передаёт его в `xray SS-relay:62640`, а для sing-box-backed протокола отправляет сразу в активный upstream. Группы с outbound `direct` или `bypass` UDP не трогают.

Общая сборка runtime живет в `xkeen-runtime.sh`: и apply из UI, и self-heal используют один и тот же код для `iptables`/`ipset`.

## Обновление без удаления

**Старую версию удалять не надо.** Рекомендуемый способ:

```sh
ssh admin@<адрес-роутера>
exec sh
/opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/update.sh | /opt/bin/sh
```

Через Keenetic Web CLI (`http://<адрес-роутера>/a`) одной строкой:

```sh
exec sh -c "opkg install curl >/dev/null 2>&1; /opt/bin/curl -fsSL https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/update.sh | /opt/bin/sh"
```

Что делает update:

1. Проверяет, что AntiGoblin уже установлен в `/opt`.
2. Если `update.sh` запущен через `curl | sh`, скачивает свежий `install.sh` из `Lagbag/AntiGoblin:main`; если скрипт запущен из распакованного релиза, использует соседний локальный `install.sh`.
3. Делает pre-upgrade backup в `/opt/var/backups/antigoblin/pre-upgrade-YYYYMMDD-HHMMSS/`. В backup попадают UI state, Xray JSON, sing-box JSON, версия и конфиг порта. Хранятся последние 5 pre-upgrade копий.
4. Обновляет UI, backend, init-скрипты, watchdog/hooks и при необходимости зависимости / Hiddify sing-box.
5. **Не перезаписывает** пользовательский state и сгенерированные routing/outbound-конфиги.
6. Перезапускает runtime и UI.

Проверить установленную версию:

```sh
cat /opt/share/xkeen-manager/VERSION
```

Если update оборвался на скачивании или `opkg`, старые пользовательские конфиги уже не стираются. Если проблема появилась после успешного обновления, нужная предыдущая копия лежит в `/opt/var/backups/antigoblin/`.

### Установка или обновление из ZIP, скачанного из чата

Если ты ещё **не залил изменения на GitHub**, распакуй ZIP на компьютере и скопируй всю папку `AntiGoblin-main` на Entware-накопитель роутера любым удобным способом. Затем в shell роутера:

```sh
cd /путь/к/AntiGoblin-main

# Если AntiGoblin уже стоит — обновление на месте, без удаления state/ключей/правил
/opt/bin/sh ./update.sh

# Если это первая установка на чистый роутер
ANTIGOBLIN_SRC_DIR="$PWD" /opt/bin/sh ./install.sh --install
```

Локальный `update.sh` автоматически замечает соседний `install.sh` и использует **эту распакованную копию**, а не `main` на GitHub. При обновлении перед заменой файлов всё равно создаётся pre-upgrade backup. Старый явный вариант `ANTIGOBLIN_SRC_DIR="$PWD" /opt/bin/sh ./install.sh --update` тоже остаётся рабочим.

## Удаление

Обычное удаление сначала сохраняет backup, снимает runtime-правила, init/cron/hooks и удаляет UI. Xray/sing-box конфиги намеренно остаются как страховка:

```sh
/opt/bin/curl -fsSL -o /opt/tmp/antigoblin-uninstall.sh \
  https://raw.githubusercontent.com/Lagbag/AntiGoblin/main/uninstall.sh
/opt/bin/sh /opt/tmp/antigoblin-uninstall.sh
```

Если нужно удалить ещё и AntiGoblin-конфиги Xray/sing-box:

```sh
/opt/bin/sh /opt/tmp/antigoblin-uninstall.sh --purge
```

`uninstall.sh` **не удаляет автоматически**:

- политику Keenetic `xkeen` — её лучше снять вручную в web UI, чтобы случайно не удалить политику с назначенными устройствами;
- `/opt/sbin/sing-box` — бинарь может использоваться другими настройками;
- каталог backup `/opt/var/backups/antigoblin/`.

После обычного удаления путь к сохранённой копии печатается в терминал.

## Разработка

Если хочется хакать локально и пушить изменения на роутер прямо с Windows:

```text
.env (gitignored)
ROUTER_SSH_PASSWORD=ssh-пароль-роутера   # required
ROUTER_HOST=192.168.1.1                  # optional, default 192.168.1.1
ROUTER_SSH_USER=root                     # optional, default root
ROUTER_SSH_PORT=22                       # optional, default 22
ANTIGOBLIN_UI_PORT=8899                  # optional, default 8899
```

Требования на dev-машине (Windows):

```powershell
# Python 3 + paramiko для router_ssh.py, которым пользуются все deploy_*.ps1
pip install paramiko

# Опционально: PowerShell-модуль Posh-SSH нужен только для xkeen_backup_state.ps1
# (остальные dev-скрипты — через router_ssh.py, только paramiko).
Install-Module Posh-SSH -Scope CurrentUser -Force
```

**Первый прогон на чистом роутере** — `bootstrap_antigoblin_router.ps1`. Он ставит essential-пакеты через opkg, создаёт политику `xkeen` в NDMS, скачивает `sing-box` по нужной архитектуре, пишет `/opt/etc/antigoblin.conf` и разворачивает init-скрипты. Без него `deploy_xkeen_manager_stack_to_router.ps1` упрётся в отсутствие `uhttpd`, политики или sing-box.

```powershell
.\scripts\xkeen\bootstrap_antigoblin_router.ps1     # first-time setup, идемпотентен
```

**Итеративный push** во время разработки — `deploy_xkeen_manager_stack_to_router.ps1`. Только заливает файлы UI/backend и рестартит uhttpd, ничего не устанавливает.

```powershell
.\scripts\xkeen\deploy_xkeen_manager_stack_to_router.ps1
```

Эти PowerShell-скрипты используют `paramiko` (Python) для SSH/SCP и не нужны конечному пользователю — он ставит всё одной командой `install.sh` через SSH на роутер. Подробнее: [docs/xkeen-manager-ui.md](docs/xkeen-manager-ui.md).

## Инварианты проекта

- `AntiGoblin` имеет право трогать только устройства из политики `xkeen`.
- Любые другие политики Keenetic (например, `no_vpn`) не должны попадать в `xray`.
- `xkeen` создается как дополнительная политика `Policy42+`, чтобы она была видна в Keenetic UI.
- UI-группы AntiGoblin не являются политиками Keenetic и не меняют назначение устройств.

## Правила проекта

- Проект должен оставаться generic для любого совместимого Keenetic.
- В репозиторий нельзя класть live-снапшоты роутера, секреты и личные черновики.
- После каждого подтвержденного бага и решения нужно обновлять [docs/troubleshooting.md](docs/troubleshooting.md).
- После изменения архитектуры нужно обновлять:
  - [README.md](README.md)
  - [docs/architecture.md](docs/architecture.md)
  - [docs/project-map.md](docs/project-map.md)

## Что под капотом и кому спасибо

`AntiGoblin` — это управляющий слой и UI поверх готовых open-source инструментов. Сам проект ничего из этих компонентов не редистрибутирует: пользователь скачивает их сам через `opkg` и `wget` с upstream-источников. Юридических обязательств у нас по их лицензиям нет, но все они достойны того, чтобы быть упомянутыми — без них этого проекта не существовало бы.

| Компонент | Роль в `AntiGoblin` | Лицензия |
|-----------|----------------------|----------|
| [XTLS/Xray-core](https://github.com/XTLS/Xray-core) | TCP transparent proxy на `:61219`. URI VLESS/VMess терминируются Xray напрямую; для sing-box-backed протоколов Xray ходит SOCKS5'ом в локальный sing-box. Локальный Shadowsocks-relay обслуживает UDP для Xray-native ключей. | MPL-2.0 |
| [hiddify/hiddify-sing-box](https://github.com/hiddify/hiddify-sing-box) / [SagerNet/sing-box](https://github.com/SagerNet/sing-box) | TPROXY UDP inbound `:61221`, mixed bridge `127.0.0.1:61225` и upstream для Hysteria(2), Trojan, SS, TUIC, AnyTLS, SOCKS/HTTP/SSH/Naive и raw standalone sing-box/Hiddify outbounds/endpoints (включая WireGuard). Installer предпочитает Hiddify build на arm64/amd64 и умеет fallback на upstream sing-box. | GPL-3.0 |
| [Entware](https://github.com/Entware/Entware) | Linux-окружение `/opt` на роутере: `opkg`, базовые утилиты, init-инфраструктура. | GPL-2.0 |
| [uhttpd_kn](https://github.com/Entware/Entware/tree/master/sources/uhttpd_kn) | HTTP-сервер, на котором живёт UI на порту `:8899`. | ISC |
| [iptables](https://www.netfilter.org/projects/iptables/) + [ipset](https://ipset.netfilter.org/) | Mark-based selective routing для устройств политики `xkeen`. | GPL-2.0 |
| [conntrack-tools](https://conntrack-tools.netfilter.org/) | Точечный сброс conntrack-записей при controlled-restart `xray` (предотвращает orphan-сокеты). | GPL-2.0 |
| [jq](https://jqlang.github.io/jq/), [gawk](https://www.gnu.org/software/gawk/), `coreutils-base64`, `net-tools-netstat` | Парсинг state.json, парсинг `ndmc`, JSON-API в backend, вспомогательные утилиты. | разные FOSS-лицензии |
| [KeeneticOS](https://help.keenetic.com/) | Базовый сетевой стек, политики маршрутизации, UI-видимая политика `xkeen`. | проприетарная (предоставляется производителем) |
| [rekryt/iplist](https://github.com/rekryt/iplist) (`iplist.opencck.org`) | Не часть стека, но рекомендуется в README как источник готовых IP/CIDR-листов для популярных сервисов. | MIT |

## Лицензия и пользовательское соглашение

Проект распространяется под лицензией [MIT](LICENSE). По ней:

- Использовать, копировать, модифицировать, встраивать в личные и коммерческие продукты — можно свободно.
- При любом использовании (и в личном проекте, и в коммерческом) нужно сохранять текст лицензии и копирайт автора. Это и есть та самая «подпись» — она уже зашита в файл [LICENSE](LICENSE), и его достаточно положить рядом с проектом или упомянуть автора в about/credits.
- Никаких гарантий нет: проект делает то, что делает. Ответственность за работу VPN-стека на конкретном роутере несет тот, кто его поставил.

Если проект пригодился — поставь, пожалуйста, ⭐ репозиторию [MaksimSamarin/AntiGoblin](https://github.com/MaksimSamarin/AntiGoblin). Это единственная просьба сверх лицензии: помогает понять, что проект кому-то нужен, и мотивирует развивать его дальше.

Если используешь в коммерческой услуге (продаёшь как часть провайдер-сервиса, ставишь клиентам за деньги, и т.п.) — кратко упомяни origin: «based on AntiGoblin by MaksimSamarin» в любом подходящем месте (about, документация, чек, договор — на твой выбор).
