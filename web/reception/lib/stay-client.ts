/** Map a Django /api/v1/... path to the reception Next.js proxy. */
export function stayProxyPath(apiV1Path: string): string {
  if (apiV1Path.startsWith("/api/stay/")) {
    return apiV1Path;
  }
  if (apiV1Path.startsWith("/api/v1/")) {
    return `/api/stay/${apiV1Path.slice("/api/v1/".length)}`;
  }
  return `/api/stay/${apiV1Path.replace(/^\//, "")}`;
}

export function reservationConfirmationPdfPath(reservationId: number): string {
  return `/api/stay/reception/reservations/${reservationId}/confirmation-pdf/`;
}

export function reservationInvoicePath(reservationId: number): string {
  return `/api/stay/reception/reservations/${reservationId}/invoice/`;
}

export function reservationInvoicePdfPath(reservationId: number): string {
  return `/api/stay/reception/reservations/${reservationId}/invoice/pdf/`;
}

export function reservationInvoiceSendEmailPath(reservationId: number): string {
  return `/api/stay/reception/reservations/${reservationId}/invoice/send-email/`;
}

export function reservationPaymentInstructionsSendPath(reservationId: number): string {
  return `/api/stay/reception/reservations/${reservationId}/payment-instructions/send/`;
}
