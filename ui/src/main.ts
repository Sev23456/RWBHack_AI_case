import { bootstrapApplication } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http'; // <--- Добавили
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async'; // <--- Для Material
import { App } from './app/app';
import { routes } from './app/app.routes';

bootstrapApplication(App, {
  providers: [
    provideRouter(routes),
    provideHttpClient(), // Решает ошибку NG0201
    provideAnimationsAsync()
  ]
}).catch(err => console.error(err));
