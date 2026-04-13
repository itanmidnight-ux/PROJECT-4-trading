import { apiError } from '@/lib/api-response';

export async function GET(request: Request) {
  return apiError(
    'NOT_FOUND',
    'Legacy proxy endpoint disabled. Use /api/trades.',
    410
  );
}
