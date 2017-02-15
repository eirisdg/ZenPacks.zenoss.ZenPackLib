##############################################################################
#
# Copyright (C) Zenoss, Inc. 2016, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################
from Acquisition import aq_base
from collections import OrderedDict
from Products.ZenModel.ThresholdGraphPoint import ThresholdGraphPoint
from ..spec.GraphPointSpec import GraphPointSpec
from .SpecParams import SpecParams


class GraphPointSpecParams(SpecParams, GraphPointSpec):
    def __init__(self, template_spec, name, **kwargs):
        SpecParams.__init__(self, **kwargs)
        self.name = name

    @classmethod
    def fromObject(cls, ob, graphdefinition):
        self = super(GraphPointSpecParams, cls).fromObject(ob)

        ob = aq_base(ob)

        threshold_graphpoints = [x for x in graphdefinition.graphPoints() if isinstance(x, ThresholdGraphPoint)]

        self.includeThresholds = False

        if threshold_graphpoints:
            thresholds = {x.id: x for x in ob.graphDef().rrdTemplate().thresholds()}
            for tgp in threshold_graphpoints:
                threshold = thresholds.get(tgp.threshId, None)
                if threshold:
                    if ob.dpName in threshold.dsnames:
                        self.includeThresholds = True

            # sort by sequence
            threshold_graphpoints = self.get_sorted_objects(threshold_graphpoints, 'sequence')
            if self.includeThresholds:
                legends = OrderedDict()
                for tgp in threshold_graphpoints:
                    legends[tgp.id] = OrderedDict()
                    legends[tgp.id]['threshId'] = tgp.threshId
                    legend = getattr(tgp, 'legend')
                    color = getattr(tgp, 'color')
                    if legend and legend != '${graphPoint/id}':
                        legends[tgp.id]['legend'] = legend
                    if color and color != '':
                        legends[tgp.id]['color'] = color
                self.thresholdLegends = legends

        return self
