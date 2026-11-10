"use client";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <section className="empty-state">
      <h1>This view could not be displayed</h1>
      <p>
        Your request may still be running on the backend. Retain its request ID
        before retrying.
      </p>
      <button className="secondary-button" onClick={reset}>
        Reload view
      </button>
    </section>
  );
}
