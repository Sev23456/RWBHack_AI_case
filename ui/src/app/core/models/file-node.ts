export interface FileNode {
  name: string;
  path: string; // полный путь к файлу
  type: 'file' | 'folder';
  children?: FileNode[];
  isExpanded?: boolean; // для UI (открыта ли папка)
}
