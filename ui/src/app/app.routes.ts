import { Routes } from '@angular/router';
import { Generator } from './pages/generator/generator';
import { Dashboard } from './pages/dashboard/dashboard'; // Проверьте, как называется класс в вашем dashboard.ts

export const routes: Routes = [
  { path: '', redirectTo: 'generator', pathMatch: 'full' },
  { path: 'generator', component: Generator },

  // Добавляем роут для сгенерированного кейса
  { path: 'demo/dashboard', component: Dashboard },

  // Отлавливаем все остальные неверные пути
  { path: '**', redirectTo: 'generator' }
];
