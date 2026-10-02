import type { DownloadableModuleFile, ModuleWithItems } from './types';

export function getDownloadableModuleFiles(module: ModuleWithItems): DownloadableModuleFile[] {
  return module.items
    .filter((item) => item.type === 'File' && typeof item.contentId === 'number')
    .map((item) => ({
      moduleId: module.id,
      moduleName: module.name,
      fileId: item.contentId!,
      fileNameHint: item.title
    }));
}

export function countUnsupportedModuleItems(module: ModuleWithItems): number {
  return module.items.filter((item) => !(item.type === 'File' && typeof item.contentId === 'number')).length;
}
