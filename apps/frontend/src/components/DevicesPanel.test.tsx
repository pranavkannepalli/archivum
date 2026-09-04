import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { DevicesPanel } from './DevicesPanel';

const device = {
  id: 'dev_abc',
  name: 'work laptop / claude',
  created_at: '2026-08-26T10:00:00Z',
  last_seen_at: '2026-08-26T11:00:00Z',
  revoked_at: null,
};

describe('DevicesPanel', () => {
  it('lists linked devices with when they were last seen', () => {
    const html = renderToString(
      <DevicesPanel devices={[device]} pairing={null} legacyKeyConfigured={false} onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).toContain('work laptop / claude');
    expect(html).toContain('dev_abc');
  });

  it('shows a revoked device as revoked rather than hiding it', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[{ ...device, revoked_at: '2026-08-26T12:00:00Z' }]}
        pairing={null}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
      />,
    );

    expect(html).toContain('Revoked');
    expect(html).toContain('work laptop / claude');
  });

  it('shows the exact command to run once a token is issued', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[]}
        pairing={{ token: 'arch1_xyz', expires_at: '2026-08-26T10:15:00Z' }}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
      />,
    );

    expect(html).toContain('npx archivum@latest connect arch1_xyz');
    expect(html).toContain('once');
    expect(html).toContain('15 minutes');
  });

  it('names the legacy shared key as a credential to retire', () => {
    const html = renderToString(
      <DevicesPanel devices={[]} pairing={null} legacyKeyConfigured onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).toContain('legacy shared key');
    expect(html).toContain('every client');
  });

  it('says nothing about a legacy key when none is configured', () => {
    const html = renderToString(
      <DevicesPanel devices={[]} pairing={null} legacyKeyConfigured={false} onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).not.toContain('legacy shared key');
  });
});
