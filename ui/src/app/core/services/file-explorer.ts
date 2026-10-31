import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http'; // Отсюда берем HttpClient
import { Observable, tap } from 'rxjs';
import { FileNode } from '../models/file-node';

@Injectable({ providedIn: 'root' })
export class FileExplorerService {
  private http = inject(HttpClient); // Инжектим его сюда

  public fileTree = signal<FileNode[]>([]);
  public selectedFileContent = signal<string>('');

  private API_URL = '/api/files';
  runId = '';

  loadTree(): Observable<FileNode[]> {
    return this.http.get<FileNode[]>(this.API_URL, { params: { run_id: this.runId } }).pipe(
      tap(tree => this.fileTree.set(tree))
    );
  }

  loadFileContent(path: string): Observable<string> {
    return this.http.get(`${this.API_URL}/content`, { params: { run_id: this.runId, path }, responseType: 'text' }).pipe(
      tap(content => this.selectedFileContent.set(content))
    );
  }
}
