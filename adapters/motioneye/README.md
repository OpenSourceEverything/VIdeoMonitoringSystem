# MotionEye Migration Adapter

MotionEyeOS is not a product platform. This boundary exists only to keep the
working legacy recorder available while the generic recorder service reaches
the consumer acceptance gate.

Any compatibility code here may translate authorized VMS cameras into legacy
motionEye configuration. It must not own identity, setup, storage, recording
policy, customer UI, or product state. New product behavior must not depend on
this adapter.
