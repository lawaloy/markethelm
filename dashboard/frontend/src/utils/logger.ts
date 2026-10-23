/** Shared browser logging. See docs/LOGGING.md for the repository conventions. */
type LogLevel = 'debug' | 'info' | 'warning' | 'error' | 'critical';

const priorities: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warning: 30,
  error: 40,
  critical: 50,
};

function minimumLevel(): number {
  const configured = String(import.meta.env.VITE_LOG_LEVEL ?? 'INFO').toLowerCase();
  return Object.prototype.hasOwnProperty.call(priorities, configured)
    ? priorities[configured as LogLevel]
    : priorities.info;
}

export function getLogger(scope: string) {
  const write = (level: LogLevel, message: string, ...details: unknown[]): void => {
    if (priorities[level] < minimumLevel()) return;
    const prefix = `${new Date().toISOString()} [${level.toUpperCase()}] ${scope}: ${message}`;
    const method = level === 'warning' ? 'warn' : level === 'critical' ? 'error' : level;
    console[method](prefix, ...details);
  };

  return {
    debug: (message: string, ...details: unknown[]) => write('debug', message, ...details),
    info: (message: string, ...details: unknown[]) => write('info', message, ...details),
    warning: (message: string, ...details: unknown[]) => write('warning', message, ...details),
    error: (message: string, ...details: unknown[]) => write('error', message, ...details),
    critical: (message: string, ...details: unknown[]) => write('critical', message, ...details),
  };
}
