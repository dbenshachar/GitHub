import { countUnsupportedModuleItems, getDownloadableModuleFiles } from '../shared/module-filters';
import type { ModuleWithItems } from '../shared/types';

interface ModuleListProps {
  modules: ModuleWithItems[];
  selectedModuleIds: Set<number>;
  onToggleModule: (moduleId: number) => void;
}

export function ModuleList({
  modules,
  selectedModuleIds,
  onToggleModule
}: ModuleListProps): JSX.Element {
  if (modules.length === 0) {
    return <div className="empty-state">No modules found in this course.</div>;
  }

  return (
    <div className="module-list">
      {modules.map((module) => {
        const downloadable = getDownloadableModuleFiles(module).length;
        const unsupported = countUnsupportedModuleItems(module);
        const isSelected = selectedModuleIds.has(module.id);

        return (
          <label className="module-item" key={module.id}>
            <input
              type="checkbox"
              checked={isSelected}
              onChange={() => onToggleModule(module.id)}
            />
            <div>
              <div className="module-name">{module.name}</div>
              <div className="module-meta">
                {downloadable} downloadable file{downloadable === 1 ? '' : 's'}
                {unsupported > 0 ? ` · ${unsupported} unsupported item${unsupported === 1 ? '' : 's'}` : ''}
              </div>
            </div>
          </label>
        );
      })}
    </div>
  );
}
