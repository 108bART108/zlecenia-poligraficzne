# Zlecenia poligraficzne — instrukcja wdrożenia

Ta apka działa jako statyczna strona (HTML/JS), a dane trzyma w Twoim
prywatnym Firestore (Firebase). Dzięki temu masz te same zlecenia
na telefonie i komputerze, a dostęp do nich ma tylko Twoje konto.

## Krok 1 — utwórz projekt Firebase

1. Wejdź na https://console.firebase.google.com i kliknij **Dodaj projekt**.
2. Nadaj dowolną nazwę (np. `zlecenia-poligraficzne`), możesz wyłączyć Google Analytics — nie jest potrzebne.
3. Poczekaj na utworzenie projektu.

## Krok 2 — włącz logowanie e-mail/hasło

1. W panelu projektu wejdź w **Build → Authentication → Get started**.
2. Zakładka **Sign-in method** → włącz **Email/Password**.
3. Przejdź do zakładki **Users** → **Add user** → wpisz swój e-mail i ustaw hasło.
   To jedyne konto, które będzie mogło się zalogować do apki.

## Krok 3 — włącz Firestore Database

1. **Build → Firestore Database → Create database**.
2. Wybierz lokalizację (np. `eur3 (europe-west)`), tryb: **produkcyjny** (production mode).
3. Po utworzeniu bazy wejdź w zakładkę **Rules** i wklej poniższe reguły,
   żeby tylko Twoje zalogowane konto miało dostęp do swoich danych:

   ```
   rules_version = '2';
   service cloud.firestore {
     match /databases/{database}/documents {
       match /users/{userId}/{document=**} {
         allow read, write: if request.auth != null && request.auth.uid == userId;
       }
     }
   }
   ```

4. Kliknij **Publish**.

## Krok 4 — pobierz konfigurację i wklej do apki

1. W panelu projektu kliknij ikonę zębatki → **Project settings**.
2. W sekcji **Your apps** kliknij ikonę `</>` (Web), zarejestruj apkę
   (dowolna nazwa, nie zaznaczaj Firebase Hosting).
3. Skopiuj obiekt `firebaseConfig` (wygląda jak poniżej):

   ```js
   const firebaseConfig = {
     apiKey: "...",
     authDomain: "...",
     projectId: "...",
     storageBucket: "...",
     messagingSenderId: "...",
     appId: "..."
   };
   ```

4. Otwórz plik `index.html` w edytorze tekstu, znajdź sekcję
   `// ====== KONFIGURACJA FIREBASE ======` i podmień wartości
   `WKLEJ_TU_...` na te skopiowane z Firebase.

   To bezpieczne, że ten config jest publicznie widoczny w kodzie strony —
   prawdziwą ochronę dają reguły Firestore z kroku 3 (tylko Twoje
   zalogowane konto może czytać/pisać swoje dane), a nie ukrywanie klucza.

## Krok 5 — wrzuć na GitHub Pages

1. Utwórz nowe, **prywatne lub publiczne** repozytorium na GitHubie
   (publiczność repo nie ma znaczenia dla bezpieczeństwa danych — patrz wyżej).
2. Wrzuć do niego całą zawartość tego folderu (`index.html`, `manifest.json`,
   `sw.js`, folder `icons/`) — najprościej przez "Add file → Upload files"
   w przeglądarce, albo `git push`.
3. W repo wejdź w **Settings → Pages**.
4. W sekcji **Build and deployment** wybierz **Deploy from a branch**,
   branch: `main`, folder: `/ (root)` → **Save**.
5. Po chwili GitHub poda adres apki, coś w stylu:
   `https://twoja-nazwa.github.io/nazwa-repo/`

## Krok 6 — zainstaluj na telefonie

1. Otwórz powyższy adres w przeglądarce na telefonie.
2. Zaloguj się kontem utworzonym w kroku 2.
3. Menu przeglądarki → **Dodaj do ekranu głównego** (Android/Chrome) lub
   **Udostępnij → Dodaj do ekranu początkowego** (iPhone/Safari).
4. Apka pojawi się jako osobna ikona i będzie otwierać się jak natywna aplikacja.

Na komputerze wystarczy wejść na ten sam adres i zalogować się tym samym
kontem — zobaczysz dokładnie te same zlecenia, na bieżąco synchronizowane.

## Uwaga

Jeśli kiedyś zapomnisz hasła albo będziesz chciał dodać/zmienić dostęp,
zrobisz to w Firebase Console → Authentication → Users.
