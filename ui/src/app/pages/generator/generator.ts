import {
  Component,
  inject,
  signal,
  OnDestroy,
  ElementRef,
  ViewChild,
  AfterViewChecked
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { FileExplorerService } from '../../core/services/file-explorer';
import { FileTreeComponent } from '../../shared/components/file-tree/file-tree';
import { FileNode } from '../../core/models/file-node';

@Component({
  selector: 'app-generator',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatTabsModule,
    MatProgressSpinnerModule,
    FileTreeComponent
  ],
  templateUrl: './generator.html',
  styleUrls: ['./generator.scss']
})
// ВАЖНО: Класс называется Generator, чтобы соответствовать app.routes.ts
export class Generator implements AfterViewChecked, OnDestroy {
  public fileService = inject(FileExplorerService);

  @ViewChild('terminalContainer') terminalContainer!: ElementRef;

  // --- Состояние через Signals (Angular 20) ---
  requirements = signal<string>('Создать мобильное приложение для отслеживания парковочных сессий...');
  logs = signal<string[]>([]);
  isRunning = signal<boolean>(false);
  completed = signal<boolean>(false);
  tokenCount = signal<number>(0);
  timeSpent = signal<number>(0);

  // Данные для дерева файлов
  generatedFiles = signal<FileNode[]>([]);

  private ws: WebSocket | null = null;
  private timerInterval: any;

  // Автоскролл терминала вниз при появлении новых логов
  ngAfterViewChecked() {
    if (this.terminalContainer && this.terminalContainer.nativeElement) {
      const el = this.terminalContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  // Очистка ресурсов при уничтожении компонента
  ngOnDestroy() {
    this.disconnectWebSocket();
    clearInterval(this.timerInterval);
  }

  startPipeline() {
    if (this.isRunning() || this.requirements().trim().length < 10) return;

    this.isRunning.set(true);
    this.completed.set(false);
    this.logs.set(['[System] Инициализация пайплайна...']);
    this.tokenCount.set(0);
    this.timeSpent.set(0);
    this.generatedFiles.set([]);
    this.fileService.selectedFileContent.set('');
    this.fileService.runId = '';

    // Таймер времени выполнения
    this.timerInterval = setInterval(() => {
      this.timeSpent.update(v => v + 1);
    }, 1000);

    // Подключение к Python WebSocket
    this.connectWebSocket();
  }

  private connectWebSocket() {
    // Укажите правильный URL вашего FastAPI (проверьте порт)
    this.ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/pipeline`);

    this.ws.onopen = () => {
      this.logs.update(l => [...l, '[System] Подключено к Python оркестратору.']);
      // Отправляем задачу на бэкенд
      this.ws?.send(JSON.stringify({ task: this.requirements(), context: 'hackathon' }));
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.run_id) this.fileService.runId = data.run_id;

        // Обработка текстовых логов от агентов
        if (data.message) {
          this.logs.update(l => [...l, `[${data.agent || 'Agent'}] ${data.message}`]);
        }

        // Обработка счетчика токенов
        if (data.tokens) {
          this.tokenCount.update(t => t + data.tokens);
        }

        // Если бэкенд сообщает о создании файлов, обновляем дерево
        if (data.type === 'file_generated' || data.status === 'completed') {
          this.fileService.loadTree().subscribe({
            next: (tree: FileNode[]) => this.generatedFiles.set(tree),
            error: (err: any) => this.logs.update(l => [...l, `[Error] Ошибка загрузки файлов: ${err.message}`])
          });
        }

        // Завершение работы
        if (data.status === 'completed' || data.status === 'error') {
          this.completed.set(data.status === 'completed');
          this.finishPipeline(data.status === 'error');
        }

      } catch (e) {
        // Если бэкенд шлет просто строки (не JSON)
        this.logs.update(l => [...l, event.data]);
      }
    };

    this.ws.onerror = (err) => {
      this.logs.update(l => [...l, '[Error] Ошибка WebSocket соединения. Проверьте, запущен ли Python.']);
      this.finishPipeline(true);
    };

    this.ws.onclose = () => {
      // Если соединение закрылось само, а пайплайн якобы работал
      if (this.isRunning()) {
        this.finishPipeline(true);
      }
    };
  }

  private finishPipeline(isError: boolean) {
    this.isRunning.set(false);
    clearInterval(this.timerInterval);
    this.disconnectWebSocket();
    this.logs.update(l => [...l, isError ? '[System] Пайплайн завершен с ошибкой.' : '[System] Пайплайн успешно завершен!']);
  }

  private disconnectWebSocket() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  // Обработка клика по файлу в дереве (вызывается из FileTreeComponent)
  onFileSelected(file: FileNode) {
    if (file.type === 'file') {
      this.logs.update(l => [...l, `[System] Запрошен файл: ${file.path}`]);
      // ИСПРАВЛЕНО: Добавлен subscribe для загрузки контента
      this.fileService.loadFileContent(file.path).subscribe({
        next: (content: string) => {
          // Здесь можно добавить логику открытия модального окна с содержимым файла
          console.log('Содержимое файла:', content);
        },
        error: (err: any) => this.logs.update(l => [...l, `[Error] Не удалось прочитать файл: ${err.message}`])
      });
    }
  }
}
