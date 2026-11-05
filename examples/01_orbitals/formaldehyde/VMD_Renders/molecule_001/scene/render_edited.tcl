# Re-render the snapshot scene/scene.vmd (e.g. after editing it
# in the VMD GUI and saving over it with File > Save State).
# scene.vmd stores absolute paths from this machine.
play scene/scene.vmd
render Tachyon tachyon/scene.dat
exit
