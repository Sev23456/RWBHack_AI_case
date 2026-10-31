import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FileNode } from '../../../core/models/file-node';

@Component({
  selector: 'app-file-tree',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './file-tree.html',
  styleUrls: ['./file-tree.scss']
})
export class FileTreeComponent {
  @Input() nodes: FileNode[] = [];
  @Output() fileClicked = new EventEmitter<FileNode>();

  toggleFolder(node: FileNode) {
    if (node.type === 'folder') {
      node.isExpanded = !node.isExpanded;
    } else {
      // Если кликнули на файл - отдаем событие родителю
      this.fileClicked.emit(node);
    }
  }
}
