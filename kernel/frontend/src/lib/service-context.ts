import type { Service } from './types';

export function serviceProject(service: Service) {
  const project = service.project;
  if (!project || typeof project !== 'object') return null;
  const repo = safeWebLink(project.repo);
  const links = Array.isArray(project.links) ? project.links.flatMap((link) => {
    const href = safeWebLink(link?.href);
    const label = typeof link?.label === 'string' ? link.label.trim() : '';
    return href && label ? [{ label, href }] : [];
  }) : [];
  return repo || links.length ? { repo, links } : null;
}

export function safeWebLink(value?: string) {
  try {
    if (typeof value !== 'string') return null;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
