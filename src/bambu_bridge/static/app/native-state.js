const START = {
  reserved: 'Start pending', queued: 'Start pending', dispatching: 'Sending start',
  sent: 'Start sent', accepted: 'Start accepted', running: 'Bridge print active',
  unknown: 'Start unconfirmed', blocked: 'Start blocked', cancelled: 'Start cancelled',
  rejected: 'Start rejected', resolved: 'Start resolved', completed: 'Print completed',
  interrupted: 'Print interrupted',
};
const FILE = {
  receiving: 'Receiving file', stored: 'Cached on bridge', delivering: 'Sending file',
  delivered: 'File on printer', failed: 'File transfer failed', external: 'External start',
};
export function startLabel(state) { return state ? START[state] || 'Start status unknown' : 'No start requested'; }
export function fileLabel(state) { return FILE[state] || 'File status unknown'; }
export function ownerLabel(page) {
  return page.start_owner
    ? page.start_owner_state ? startLabel(page.start_owner_state) : 'Bridge start in progress'
    : 'No bridge start in progress';
}
