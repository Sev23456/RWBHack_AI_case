import { Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, MatCardModule],
  templateUrl: './dashboard.html',
  styleUrl: './dashboard.scss'
})
export class Dashboard {
  sessions = signal([
    { id: 1, plate: 'А123ВС78', zone: 'Варшавское шоссе, 125', end: '12:15', status: 'active' },
    { id: 2, plate: 'В456ЕК98', zone: 'Сеславинская, 42', end: '11:00', status: 'warning' }
  ]);
}
