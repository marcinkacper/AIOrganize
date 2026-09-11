# Plan wdrożenia: Antigravity CLI – 12 trwałych profili i wspólne rozmowy

## 1. Cel

Utworzyć środowisko dla 12 legalnie używanych kont Google AI Plus, w którym każde konto pozostaje stale zalogowane do Antigravity CLI, a użytkownik może szybko przełączyć profil i wznowić tę samą lokalną rozmowę przez jej UUID.

Projekt nie kopiuje tokenów OAuth między kontami. Każdy profil posiada własne poświadczenia. Przełączenie rozmowy odbywa się dopiero po poprawnym zatrzymaniu poprzedniej instancji.

## 2. Potwierdzony stan Antigravity CLI 1.2.0

- token konta: `~/.gemini/antigravity-cli/antigravity-oauth-token`,
- rozmowy: `~/.gemini/antigravity-cli/conversations/<UUID>.db`,
- aktywna baza może posiadać pliki `<UUID>.db-wal` i `<UUID>.db-shm`,
- adnotacje: `~/.gemini/antigravity-cli/annotations/<UUID>.pbtxt`,
- ustawienia: `~/.gemini/antigravity-cli/settings.json`,
- metadane i ostatnie rozmowy: `~/.gemini/antigravity-cli/cache/`,
- blokady bieżących procesów: `~/.gemini/antigravity-cli/presence/`,
- natywne wznowienie ostatniej rozmowy: `agy --continue`,
- natywne wznowienie wskazanej rozmowy: `agy --conversation <UUID>`,
- lista modeli dostępnych dla profilu: `agy models`.

## 3. Architektura katalogów

```text
/srv/agy-manager/
├── profiles/
│   ├── account-01/home/.gemini/
│   ├── account-02/home/.gemini/
│   └── ...
│       account-12/home/.gemini/
├── shared/
│   ├── conversations/
│   ├── annotations/
│   └── locks/
├── manager/
├── logs/
└── data/manager.db
```

Każdy profil zachowuje osobne wyłącznie:

- `antigravity-oauth-token`, ponieważ ten plik identyfikuje konto Google,
- techniczne pliki tymczasowe procesu i osobny plik logu, jeżeli równoległe procesy nie mogą ich bezpiecznie współdzielić.

Wspólne dla wszystkich profili mają być wszystkie elementy, które przejdą test równoległego dostępu:

- `settings.json` – te same ustawienia, tryb pracy, model domyślny i zaufane katalogi,
- `.gemini/config/config.json`,
- `.gemini/config/mcp_config.json`,
- `.gemini/config/projects/` – wspólne projekty,
- `conversations/` – wspólne bazy rozmów,
- `annotations/` – wspólne adnotacje,
- `presence/` – wspólna widoczność uruchomionych rozmów,
- `cache/conversation_metadata.json` i lista rozmów, jeżeli test potwierdzi bezpieczne zapisy,
- wtyczki, MCP, agenci i inne ustawienia użytkowe AGY,
- rejestr blokad rozmów zarządzany przez menedżera.

Profile będą zbudowane jako cienkie katalogi zawierające własny token OAuth i dowiązania symboliczne do wspólnego środowiska AGY. Dzięki temu zmiana ustawienia, uprawnienia, serwera MCP albo projektu w jednym profilu będzie widoczna w pozostałych.

Pliki cache, które AGY nadpisuje operacją typu „zapis do pliku tymczasowego i zmiana nazwy”, wymagają osobnego testu, ponieważ taka operacja może zastąpić samo dowiązanie symboliczne. Jeśli cały wspólny katalog `cache/` nie przejdzie testu równoległości, wspólne pozostaną jego dane merytoryczne, a chwilowy cache interfejsu zostanie per profil. Nie wpływa to na możliwość wznowienia rozmowy przez `--conversation <UUID>`.

## 4. Zasada bezpieczeństwa rozmów

Jedna rozmowa może być otwarta przez maksymalnie jeden profil jednocześnie.

Menedżer przed uruchomieniem rozmowy:

1. sprawdza lock UUID,
2. zapisuje właściciela: profil, PID, tmux session i czas,
3. uruchamia `agy --conversation <UUID>`,
4. obserwuje proces,
5. usuwa lock dopiero po zakończeniu procesu i zamknięciu SQLite.

Przełączenie konta wykonuje kolejno:

1. prośbę o łagodne zamknięcie poprzedniego procesu,
2. oczekiwanie na zakończenie PID,
3. sprawdzenie, czy pliki `-wal` i `-shm` nie są już używane,
4. zwolnienie locka UUID,
5. uruchomienie tego samego UUID z nowym profilem,
6. weryfikację, że rozmowa została poprawnie załadowana.

Nie wolno zabijać procesu `kill -9`, kopiować aktywnej bazy zwykłym `cp` ani uruchamiać dwóch instancji na tym samym UUID.

## 5. Etapy realizacji

### Etap A – diagnostyka i kopia bezpieczeństwa

- ustalić absolutną ścieżkę binarki `agy`,
- sprawdzić właściciela i uprawnienia tokenu,
- wykonać kopię katalogu `.gemini` przy zamkniętym Antigravity,
- odnotować aktualne UUID rozmów,
- sprawdzić schemat jednej kopii bazy SQLite bez odczytywania sekretów,
- zweryfikować zachowanie `--conversation` na istniejącym koncie.

### Etap B – pilotaż dwóch profili

- utworzyć `account-01` i `account-02`,
- przenieść obecne konto do `account-01`,
- zalogować konto drugie w izolowanym `HOME`,
- potwierdzić osobne tokeny i działanie `agy models`,
- utworzyć wspólne ustawienia, konfigurację, projekty, rozmowy, adnotacje i presence,
- przetestować wspólny cache i automatyczne odświeżanie listy rozmów,
- uruchomić jedną testową rozmowę na profilu 1,
- poprawnie ją zamknąć i wznowić tym samym UUID na profilu 2,
- sprawdzić spójność historii, narzędzi, projektu i dalszej odpowiedzi.

### Etap C – skrypt menedżera

Przygotować polecenia:

```text
agy-manager profiles
agy-manager models <profil>
agy-manager conversations
agy-manager status
agy-manager start <profil> [UUID]
agy-manager switch <profil> <UUID>
agy-manager stop <UUID>
agy-manager attach <profil>
agy-manager unlock <UUID> --only-if-stale
```

Menedżer ma używać bezpiecznych wywołań procesów bez `shell=True`, walidować nazwę profilu oraz UUID i nigdy nie wyświetlać zawartości tokenów.

### Etap D – tmux

- jedna nazwana sesja tmux dla każdego aktywnego profilu,
- możliwość wejścia do pojedynczej sesji na pełnym ekranie,
- opcjonalny podgląd 2×2 wybranych profili,
- procesy pozostają aktywne po rozłączeniu SSH/noVNC,
- tytuł okna pokazuje profil, UUID i model.

### Etap E – panel WWW

Panel FastAPI + HTML/CSS/JavaScript:

- 12 kart profili,
- stan zalogowania bez ujawniania tokenu,
- dostępne modele z `agy models`,
- aktywny UUID i PID,
- ostatnia aktywność,
- przyciski Start, Otwórz terminal, Zatrzymaj i Przełącz,
- lista rozmów z metadanych lokalnych,
- ostrzeżenie przy próbie jednoczesnego otwarcia UUID,
- dziennik operacji bez promptów, tokenów i danych OAuth.

### Etap E2 – test wspólnych ustawień i uprawnień

- zmienić bezpieczne ustawienie AGY na profilu 1 i potwierdzić je na profilu 2,
- dodać testowy serwer MCP i potwierdzić widoczność w obu profilach,
- zmienić uprawnienie narzędzia i potwierdzić wspólny rezultat,
- sprawdzić jednoczesny zapis ustawień przez dwa procesy,
- potwierdzić, że żadna operacja nie zastępuje dowiązań symbolicznych lokalną kopią,
- w przypadku kolizji zastosować centralny zapis przez menedżera i przeładowanie procesów.

### Etap F – rozszerzenie na 12 kont

- utworzyć profile 03–12,
- zalogować każde konto tylko w jego własnym `HOME`,
- ustawić uprawnienia katalogów `0700` i plików sekretów `0600`,
- wykonać test przełączania na każdym profilu,
- dodać automatyczny start menedżera po restarcie serwera,
- nie uruchamiać automatycznie interaktywnych rozmów bez decyzji użytkownika.

## 6. Monitorowanie dostępności

Można bezpiecznie monitorować:

- czy profil jest zalogowany,
- wynik `agy models`,
- model używany przez bieżącą sesję,
- błędy autoryzacji,
- komunikaty o czasowej niedostępności lub limicie,
- czas ostatniej udanej odpowiedzi.

Dokładny licznik pozostałych tokenów będzie pokazywany tylko wtedy, gdy Antigravity CLI 1.2.0 faktycznie udostępnia go w stabilnym wyjściu lub lokalnych metadanych. Nie należy wyliczać fikcyjnej wartości.

## 7. Testy akceptacyjne

- wszystkie 12 profili pozostają niezależnie zalogowane,
- przełączenie profilu nie modyfikuje tokenu innego konta,
- ustawienia, uprawnienia, MCP i projekty są wspólne dla wszystkich profili,
- jedynym trwałym elementem identyfikującym profil jest jego własny token OAuth,
- rozmowa o wskazanym UUID ładuje się na drugim profilu,
- historia pozostaje kompletna po minimum 20 przełączeniach,
- nie występują błędy SQLite ani uszkodzenia `db`, `wal`, `shm`,
- menedżer blokuje równoległe uruchomienie jednego UUID,
- restart serwera nie usuwa profili ani wspólnej historii,
- panel nie ujawnia tokenów, treści promptów ani sekretów,
- awaria procesu pozostawia możliwą do rozpoznania i bezpiecznego zwolnienia blokadę.

## 8. Plan awaryjny

Jeżeli test pokaże, że rozmowa jest kryptograficznie lub serwerowo przypisana do konta Google, wspólne katalogi pozostaną magazynem historii, lecz przełączenie będzie korzystało z automatycznego pliku przekazania zadania zamiast kontynuowania tego samego UUID. Nie rozszerzamy rozwiązania na 12 kont, dopóki pilotaż dwóch profili nie przejdzie bez utraty historii.

## 9. Kolejność najbliższych prac

1. Wykonać kopię bezpieczeństwa obecnego profilu.
2. Utworzyć dwa izolowane profile testowe.
3. Sprawdzić `agy models` w obu profilach.
4. Zbudować wspólne ustawienia, konfigurację, projekty, rozmowy, adnotacje i presence.
5. Przetestować współdzielenie cache przy dwóch równoległych procesach.
6. Zaimplementować lock UUID i kontrolowane zamykanie.
7. Przeprowadzić test przełączenia jednej rozmowy bez kopiowania jej plików.
8. Dopiero po powodzeniu przygotować menedżera, tmux, panel i profile 03–12.
