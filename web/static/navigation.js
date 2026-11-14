const pages = new Set(['dashboard', 'tasks', 'chat', 'accounts', 'settings']);

export function routeFor(pathname, hash) {
  const [name, query = ''] = hash.replace(/^#/, '').split('?');
  const params = new URLSearchParams(query);
  if (pages.has(name)) return {page: name, task: params.get('task'), kind: params.get('kind') || 'all'};
  const legacy = {'/work': 'work', '/two-stage': 'two', '/abc': 'abc', '/ai-workflow': 'ai'};
  if (legacy[pathname]) return {page: 'tasks', kind: legacy[pathname], preview: name === 'message-preview'};
  if (['/wechat', '/wechat2'].includes(pathname)) return {page: 'accounts', kind: 'all'};
  if (pathname === '/accounts') return {page: 'accounts', kind: 'all'};
  return {page: 'dashboard', kind: 'all'};
}

export function hashFor(page, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value));
  return '#' + page + (query.size ? '?' + query : '');
}
