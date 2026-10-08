//! The remaining lease of an already admitted view, never proof of a saved edit.
#[derive(Clone, Copy, Default)]
pub(crate) struct PreviewLease(Option<f64>);

impl PreviewLease {
    pub fn admitted(&mut self, expires: f64) {
        self.0 = expires.is_finite().then_some(expires);
    }

    /// A newer canonical version is being fetched. Keep the old native preview only until
    /// its original transport lease ends; cursor heartbeats cannot extend this handoff.
    pub fn retain_for_install(self, clock: f64, server_revision: i64, installed_revision: i64, clean: bool) -> bool {
        clean && server_revision > installed_revision && self.0.is_some_and(|until| clock.is_finite() && clock < until)
    }

    pub fn expired(self, clock: f64) -> bool {
        self.0.is_none_or(|until| !clock.is_finite() || clock >= until)
    }
}

#[cfg(test)]
mod tests {
    use super::PreviewLease;

    #[test]
    fn pending_install_retains_only_original_remaining_lease() {
        let mut lease = PreviewLease::default();
        assert!(!lease.retain_for_install(100., 2, 1, true));
        lease.admitted(1900.); // The admitted snapshot already paid its request elapsed time.
        for clock in [100., 500., 1500., 1899.] {
            assert!(lease.retain_for_install(clock, 2, 1, true));
            assert!(!lease.expired(clock));
        }
        assert!(!lease.retain_for_install(1900., 2, 1, true));
        assert!(lease.expired(1900.));
    }

    #[test]
    fn canonical_install_local_edits_and_same_base_clear_instead_of_retaining() {
        let mut lease = PreviewLease::default();
        lease.admitted(1900.);
        assert!(!lease.retain_for_install(500., 2, 2, true), "canonical installed");
        assert!(!lease.retain_for_install(500., 2, 1, false), "local document edited");
        assert!(!lease.retain_for_install(500., 1, 1, true), "same-base Cancel or downgrade");
        assert!(!lease.retain_for_install(500., 0, 1, true), "old response");
        assert!(!lease.retain_for_install(f64::NAN, 2, 1, true));
        lease.admitted(f64::INFINITY);
        assert!(!lease.retain_for_install(500., 2, 1, true));
    }
}
