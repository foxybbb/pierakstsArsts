# Ārsta pieraksta monitorings (eveselibaspunkts.lv → Telegram)

Pēc grafika pārbauda tuvāko brīvo pieraksta laiku (valsts apmaksāts / maksas) pie izvēlētajiem ārstiem
un nosūta ziņu uz Telegram, kad parādās brīvs laiks vai atbrīvojas agrāks laiks.

## Palaišana
1. Izveidojiet botu pie @BotFather, uzrakstiet tam jebkādu ziņu un uzziniet chat_id:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → `message.chat.id`
   (grupai ID ir negatīvs, piemēram, `-5262503769`).
2. `cp .env.example .env` un aizpildiet tokenu un chat_id.
3. Ārstus un pārbaužu laikus norādiet `config/config.yaml` (izmaiņas tiek nolasītas bez restartēšanas).
4. `docker compose up -d --build`, žurnāli: `docker compose logs -f`.

## Kā tas darbojas
Lapa ir Vue lietotne; dati nāk no `POST /lv/Booking/ListSpecialistCalendars`
(nepieciešams Laravel CSRF: sīkdatne `XSRF-TOKEN` → galvene `X-XSRF-TOKEN`). Skripts to dara ar parastu HTTP;
ja neizdodas (piemēram, Cloudflare), automātiski tiek izmantots headless Chromium (Playwright).

## Paziņojumi
Pēc noklusējuma ziņa tiek sūtīta **tikai tad, kad parādās pieraksta laiks** (vai agrāks laiks).
Papildu ziņas var ieslēgt `config/config.yaml`:

| Iestatījums | Nozīme |
|---|---|
| `notify_types` | laika veidi: `gov` (valsts apmaksāts), `paid` (maksas) |
| `notify_lost` | ziņot, kad brīvie laiki pazuduši |
| `notify_unchanged` | sūtīt kopsavilkumu arī bez izmaiņām |
| `notify_startup` | sūtīt kopsavilkumu pirmajā palaišanā |
| `notify_errors` | sūtīt datu ielādes kļūdas (citādi tikai žurnālā) |

## Bota komandas
`/check` — pārbaudīt tagad (rāda arī kļūdas), `/status` — pēdējais stāvoklis,
`/history` — kurās stundās/dienās datubāze reāli mainās, `/schedule` — pārbaužu grafiks.

## Kad atjaunojas datubāze
Iestādes nepublicē jauno laiku atvēršanas grafiku. RAKUS pieraksta ~6 mēnešu slīdošā periodā,
tātad jauni valsts apmaksātie laiki atveras pakāpeniski; atceltie pieraksti atbrīvojas jebkurā brīdī.
Tāpēc pirmo nedēļu iestatiet `interval_minutes: 30`, pēc tam apskatiet `/history`
un `check_times` atstājiet tikai "karstās" stundas.

Dati: `data/state.json` (pēdējais stāvoklis), `data/history.jsonl` (visas izmaiņas ar laiku).
